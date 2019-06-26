import gevent.server
import gevent.queue
import gevent.socket
from RLTest import Env
import redis
import struct
import random
from connection import Connection
import time

class ConnectionProxy():
    def __init__(self, env, conn, port, fromId, toId):
        self.fromId = fromId
        self.toId = toId
        self.port = port
        self.toConn = conn
        self.env = env
        self.pending_requests = gevent.queue.Queue()
        self.stream_server = gevent.server.StreamServer(('localhost', port), self._handle_conn)
        self.stream_server.start()

    def _handle_conn(self, sock, client_addr):
        self.env.debugPrint('new connection to shard %d' % self.toId)
        self.fromConn = Connection(sock)
        self.fromConn.handle_requests(self.pending_requests)

    def Stop(self):
        self.stream_server.stop()

    def PassMessages(self):
        isMessagesPassed = False
        try:
            while True:
                req = self.pending_requests.get(block=True, timeout=0.1)
                isMessagesPassed = True
                printableReq = req
                if req[0] == 'rg.innermsgcommand':
                    try:
                        InvokeFunction = req[2]
                        consensusNameLen = struct.unpack('<ll', req[3][0:8])[0]
                        ConsensusName = req[3][8:8 + consensusNameLen - 1]
                        InstanceId = struct.unpack('<ll', req[3][8 + consensusNameLen: 16 + consensusNameLen])[0]
                        ProposalId = struct.unpack('<ll', req[3][16 + consensusNameLen: 24 + consensusNameLen])[0]
                        printableReq = '%s - name:%s, id:%d, proposal:%d' % (InvokeFunction, ConsensusName, InstanceId, ProposalId)
                    except Exception as e:
                        print e

                self.env.debugPrint('request  : %d -> %d : %s' % (self.fromId, self.toId, str(printableReq)))
                res = self.toConn.execute_command(*req)
                if res == 'OK':
                    self.fromConn.send_status(res)
                else:
                    self.fromConn.send_bulk(res)
        except Exception as e:
            pass
        return isMessagesPassed

class ShardProxy():
    def __init__(self, env, shardId, initPort):
        self.env = env
        self.shardId = shardId
        self.shardConn = self.env.getConnection(self.shardId)
        self.proxies = {}
        for s in range(1, env.shardsCount + 1):
            if shardId == s:
                continue
            self.sConn = env.getConnection(s)
            self.proxies[s] = ConnectionProxy(env, self.sConn, initPort, self.shardId, s)
            initPort += 2

    def Stop(self):
        for p in self.proxies.values():
            p.Stop()

    def PassMessagesTo(self, *ids):
        isMessagesPassed = False
        for i in ids:
            isMessagesPassed |= self.proxies[i].PassMessages()
        return isMessagesPassed

    def Execute(self, *command):
        return self.shardConn.execute_command(*command)

    def SendClusterSetMessage(self):
        msg = ['RG.CLUSTERSET',
               'NO-USED',
               'NO-USED',
               'NO-USED',
               'NO-USED',
               'NO-USED',
               str(self.shardId),
               'NO-USED',
               self.env.shardsCount]
        number_of_slots_in_bdb = 16384
        slots_per_shard = float(number_of_slots_in_bdb) / self.env.shardsCount
        last_slot = number_of_slots_in_bdb - 1
        start_slots = [int(round(i * slots_per_shard)) for i in range(self.env.shardsCount)]
        end_slots = [next_start_slot - 1 for next_start_slot in start_slots[1:]] + [last_slot]
        for i in range(1, self.env.shardsCount + 1):
            msg += ['NO-USED',
                    '%d' % (i), # shard id
                    'NO-USED',
                    '%d' % start_slots[i - 1], # first hslot
                    '%d' % end_slots[i - 1], # last hslot
                    'NO-USED',
                    'localhost:%d' % (self.proxies[i].port if i in self.proxies.keys() else 0), # addr + password
                    'NO-USED'] # hash policy (optional unix socket should be added here!!!)
        self.env.debugPrint('send cluster set to shard %d : %s' % (self.shardId, msg))
        self.shardConn.execute_command(*msg)

class ProxyManager():
    def __init__(self, env):
        self.env = env

    def __enter__(self):
        self.shardsProxies = []
        initPort = 10000
        for shard in range(self.env.shardsCount):
            self.shardsProxies.append(ShardProxy(self.env, shard + 1, initPort))
            initPort += 100

        for shardProxy in self.shardsProxies:
            shardProxy.SendClusterSetMessage()

        self.PassMessages()

        return self

    def __exit__(self, type, value, traceback):
        for shardProxy in self.shardsProxies:
            shardProxy.Stop()


    def Shard(self, id):
        return self.shardsProxies[id - 1]

    def PassMessages(self):
        IsMessagesSent = False
        for i in range(1, len(self.shardsProxies) + 1):
            proxies = [j for j in range(1, len(self.shardsProxies) + 1) if j != i]
            IsMessagesSent |= self.Shard(i).PassMessagesTo(*proxies)
        return IsMessagesSent

    def PassAllMessages(self):
        while self.PassMessages():
            pass

def testTermination():
    env = Env(env='oss-cluster', shardsCount=7, moduleArgs='ConsensusIdleIntervalOnFailure 1000-4000')
    with ProxyManager(env) as pm:
        pm.Shard(1).Execute('rg.testconsensusset', 'foo1')
        pm.Shard(2).Execute('rg.testconsensusset', 'foo2')
        pm.Shard(3).Execute('rg.testconsensusset', 'foo3')

        val1 = None
        while val1 is None:
            pm.PassAllMessages()

            val1 = pm.Shard(1).Execute('rg.testconsensusget')
            val2 = pm.Shard(2).Execute('rg.testconsensusget')
            val3 = pm.Shard(3).Execute('rg.testconsensusget')

        pm.PassAllMessages()
        env.assertEqual(val1, val2)
        env.assertEqual(val1, val3)

def testSimple():
    env = Env(env='oss-cluster', shardsCount=3 ,moduleArgs='ConsensusIdleIntervalOnFailure 0-0')
    with ProxyManager(env) as pm:
        pm.Shard(1).Execute('rg.testconsensusset', 'foo')
        pm.Shard(2).Execute('rg.testconsensusset', 'bar')
        env.assertEqual(pm.Shard(1).Execute('rg.testconsensusget'), None)
        env.assertEqual(pm.Shard(2).Execute('rg.testconsensusget'), None)
        env.assertEqual(pm.Shard(3).Execute('rg.testconsensusget'), None)

        # achieve consensus on a value require majority who talk to each other for 2 phases
        pm.Shard(1).PassMessagesTo(3) ## phase 1 message
        pm.Shard(3).PassMessagesTo(1) ## phase 1 reply
        pm.Shard(1).PassMessagesTo(3) ## phase 2 message
        pm.Shard(3).PassMessagesTo(1) ## phase 2 reply + learn

        # value did not yet arrived to Node 2
        env.assertEqual(pm.Shard(1).Execute('rg.testconsensusget'), 'foo')
        env.assertEqual(pm.Shard(2).Execute('rg.testconsensusget'), None)
        env.assertEqual(pm.Shard(3).Execute('rg.testconsensusget'), 'foo')

        # None 2 should learned the 'foo' value immidiatly (cause he will get the learned messages)
        pm.Shard(1).PassMessagesTo(2)
        pm.Shard(3).PassMessagesTo(2)

        env.assertEqual(pm.Shard(2).Execute('rg.testconsensusget'), 'foo')

        # node 2 should start another consensus cause it will see that it failed to achieve consensus the first time
        pm.PassAllMessages()

        env.assertEqual(pm.Shard(1).Execute('rg.testconsensusget'), 'bar')
        env.assertEqual(pm.Shard(2).Execute('rg.testconsensusget'), 'bar')
        env.assertEqual(pm.Shard(3).Execute('rg.testconsensusget'), 'bar')

def testSimple2():
    env = Env(env='oss-cluster', shardsCount=3, moduleArgs='ConsensusIdleIntervalOnFailure 0-0')
    with ProxyManager(env) as pm:
        pm.Shard(1).Execute('rg.testconsensusset', 'foo')
        pm.Shard(2).Execute('rg.testconsensusset', 'bar')

        # shard 1 achieved majority
        pm.Shard(1).PassMessagesTo(3) ## phase 1 message
        pm.Shard(3).PassMessagesTo(1) ## phase 1 reply

        # node 2 recruited node 3 before node 1 sent the accept value message
        pm.Shard(2).PassMessagesTo(3) # try to recruited node 3 but failed
        pm.Shard(3).PassMessagesTo(2)

        pm.Shard(2).PassMessagesTo(3) # try again and succed

        # node 1 send the accept message which will be denied by node 3
        pm.Shard(1).PassMessagesTo(3) ## accept message
        pm.Shard(3).PassMessagesTo(1) ## denied

        # no value has yet been accepted
        env.assertEqual(pm.Shard(1).Execute('rg.testconsensusget'), None)
        env.assertEqual(pm.Shard(2).Execute('rg.testconsensusget'), None)
        env.assertEqual(pm.Shard(3).Execute('rg.testconsensusget'), None)

        # node 1 raises the proposal number and try again
        pm.Shard(1).PassMessagesTo(3) ## try to recruited 3 but failed cause its already been recruited by 2
        pm.Shard(3).PassMessagesTo(1) ## deny message

        pm.Shard(1).PassMessagesTo(3) ## try again and succed
        pm.Shard(3).PassMessagesTo(1)
        pm.Shard(1).PassMessagesTo(3) ## send accept message
        pm.Shard(3).PassMessagesTo(1) ## value accepted and learn

        # value did not yet arrived to Node 2
        env.assertEqual(pm.Shard(1).Execute('rg.testconsensusget'), 'foo')
        env.assertEqual(pm.Shard(2).Execute('rg.testconsensusget'), None)
        env.assertEqual(pm.Shard(3).Execute('rg.testconsensusget'), 'foo')

        # None 2 should learned the 'foo' value immidiatly (cause he will get the learned messages)
        pm.Shard(1).PassMessagesTo(2)
        pm.Shard(3).PassMessagesTo(2)

        env.assertEqual(pm.Shard(2).Execute('rg.testconsensusget'), 'foo')

        # node 2 should start another consensus cause it will see that it failed to achieve consensus the first time
        pm.PassAllMessages()

        env.assertEqual(pm.Shard(1).Execute('rg.testconsensusget'), 'bar')
        env.assertEqual(pm.Shard(2).Execute('rg.testconsensusget'), 'bar')
        env.assertEqual(pm.Shard(3).Execute('rg.testconsensusget'), 'bar')

def testRandom():
    env = Env(env='oss-cluster', shardsCount=3)
    with ProxyManager(env) as pm:
        i = 1
        pm.Shard(1).Execute('rg.testconsensusset', 'foo%d' % i)
        i += 1
        pm.Shard(2).Execute('rg.testconsensusset', 'foo%d' % i)
        i += 1
        pm.Shard(3).Execute('rg.testconsensusset', 'foo%d' % i)
        i += 1

        for _ in range(1000):
            fromShard = random.randint(1, 3)
            if(random.randint(1, 2) == 1):
                pm.Shard(3).Execute('rg.testconsensusset', 'foo%d' % i)
                i += 1
            else:
                toShard = [j for j in range(1, 4) if j != fromShard][random.randint(0, 1)]
                pm.Shard(fromShard).PassMessagesTo(toShard)

        pm.PassAllMessages()

        va1 = pm.Shard(1).Execute('rg.testconsensusget')
        va2 = pm.Shard(2).Execute('rg.testconsensusget')
        va3 = pm.Shard(3).Execute('rg.testconsensusget')
        env.assertEqual(va1, va2)
        env.assertEqual(va1, va3)