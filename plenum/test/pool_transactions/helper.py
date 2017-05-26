from stp_core.types import HA
from typing import Iterable, Union

from plenum.client.client import Client
from plenum.client.wallet import Wallet
from plenum.common.constants import STEWARD, TXN_TYPE, NYM, ROLE, TARGET_NYM, ALIAS, \
    NODE_PORT, CLIENT_IP, NODE_IP, DATA, NODE, CLIENT_PORT, VERKEY, SERVICES, \
    VALIDATOR
from plenum.common.keygen_utils import initNodeKeysForBothStacks
from plenum.common.signer_simple import SimpleSigner
from plenum.common.util import randomString, hexToFriendly
from plenum.test.helper import waitForSufficientRepliesForRequests
from plenum.test.test_client import TestClient, genTestClient
from plenum.test.test_node import TestNode, check_node_disconnected_from, \
    ensure_node_disconnected, checkNodesConnected
from stp_core.loop.eventually import eventually
from stp_core.network.port_dispenser import genHa


def sendAddNewClient(role, name, creatorClient, creatorWallet):
    wallet = Wallet(name)
    wallet.addIdentifier()
    idr = wallet.defaultId

    op = {
        TXN_TYPE: NYM,
        TARGET_NYM: idr,
        ALIAS: name,
        VERKEY: wallet.getVerkey(idr)
    }

    if role:
        op[ROLE] = role

    req = creatorWallet.signOp(op)
    creatorClient.submitReqs(req)
    return req, wallet


def addNewClient(role, looper, creatorClient: Client, creatorWallet: Wallet,
                 name: str):
    req, wallet = sendAddNewClient(role, name, creatorClient, creatorWallet)
    waitForSufficientRepliesForRequests(looper, creatorClient,
                                        requests=[req], fVal=1)

    return wallet


def sendAddNewNode(newNodeName, stewardClient, stewardWallet,
                   transformOpFunc=None):
    sigseed = randomString(32).encode()
    nodeSigner = SimpleSigner(seed=sigseed)
    (nodeIp, nodePort), (clientIp, clientPort) = genHa(2)

    op = {
        TXN_TYPE: NODE,
        TARGET_NYM: nodeSigner.identifier,
        DATA: {
            NODE_IP: nodeIp,
            NODE_PORT: nodePort,
            CLIENT_IP: clientIp,
            CLIENT_PORT: clientPort,
            ALIAS: newNodeName,
            SERVICES: [VALIDATOR, ]
        }
    }
    if transformOpFunc is not None:
        transformOpFunc(op)

    req = stewardWallet.signOp(op)
    stewardClient.submitReqs(req)
    return req, \
           op[DATA].get(NODE_IP), op[DATA].get(NODE_PORT), \
           op[DATA].get(CLIENT_IP), op[DATA].get(CLIENT_PORT), \
           sigseed


def addNewNode(looper, stewardClient, stewardWallet, newNodeName, tdir, tconf,
               allPluginsPath=None, autoStart=True, nodeClass=TestNode):
    req, nodeIp, nodePort, clientIp, clientPort, sigseed \
        = sendAddNewNode(newNodeName, stewardClient, stewardWallet)
    waitForSufficientRepliesForRequests(looper, stewardClient,
                                        requests=[req], fVal=1)

    initNodeKeysForBothStacks(newNodeName, tdir, sigseed, override=True)
    node = nodeClass(newNodeName, basedirpath=tdir, config=tconf,
                     ha=(nodeIp, nodePort), cliha=(clientIp, clientPort),
                     pluginPaths=allPluginsPath)
    if autoStart:
        looper.add(node)
    return node


def addNewSteward(looper, tdir,
                  creatorClient, creatorWallet, stewardName,
                  clientClass=TestClient):
    newStewardWallet = addNewClient(STEWARD, looper, creatorClient,
                                    creatorWallet, stewardName)
    newSteward = clientClass(name=stewardName,
                             nodeReg=None, ha=genHa(),
                             basedirpath=tdir)

    looper.add(newSteward)
    looper.run(newSteward.ensureConnectedToNodes())
    return newSteward, newStewardWallet


def addNewStewardAndNode(looper, creatorClient, creatorWallet, stewardName,
                         newNodeName, tdir, tconf, allPluginsPath=None,
                         autoStart=True, nodeClass=TestNode,
                         clientClass=TestClient):

    newSteward, newStewardWallet = addNewSteward(looper, tdir, creatorClient,
                                                 creatorWallet, stewardName,
                                                 clientClass=clientClass)

    newNode = addNewNode(looper, newSteward, newStewardWallet, newNodeName,
                         tdir, tconf, allPluginsPath, autoStart=autoStart,
                         nodeClass=nodeClass)
    return newSteward, newStewardWallet, newNode


def sendUpdateNode(stewardClient, stewardWallet, node, node_data):
    nodeNym = hexToFriendly(node.nodestack.verhex)
    op = {
        TXN_TYPE: NODE,
        TARGET_NYM: nodeNym,
        DATA: node_data,
    }

    req = stewardWallet.signOp(op)
    stewardClient.submitReqs(req)
    return req


def updateNodeData(looper, stewardClient, stewardWallet, node, node_data):
    req = sendUpdateNode(stewardClient, stewardWallet, node, node_data)
    waitForSufficientRepliesForRequests(looper, stewardClient,
                                        requests=[req], fVal=1)
    # TODO: Not needed in ZStack, remove once raet is removed
    node.nodestack.clearLocalKeep()
    node.nodestack.clearRemoteKeeps()
    node.clientstack.clearLocalKeep()
    node.clientstack.clearRemoteKeeps()


def updateNodeDataAndReconnect(looper, steward, stewardWallet, node,
                               node_data,
                               tdirWithPoolTxns, tconf, txnPoolNodeSet):
    updateNodeData(looper, steward, stewardWallet, node, node_data)
    # restart the Node with new HA
    node.stop()
    node_alias = node_data.get(ALIAS, None) or node.name
    node_ip = node_data.get(NODE_IP, None) or node.nodestack.ha.host
    node_port = node_data.get(NODE_PORT, None) or node.nodestack.ha.port
    client_ip = node_data.get(CLIENT_IP, None) or node.clientstack.ha.host
    client_port = node_data.get(CLIENT_PORT, None) or node.clientstack.ha.port
    looper.removeProdable(name=node.name)
    restartedNode = TestNode(node_alias, basedirpath=tdirWithPoolTxns,
                             config=tconf,
                             ha=HA(node_ip, node_port),
                             cliha=HA(client_ip, client_port))
    looper.add(restartedNode)

    # replace node in txnPoolNodeSet
    try:
        idx = next(i for i, n in enumerate(txnPoolNodeSet)
                   if n.name == node.name)
    except StopIteration:
        raise Exception('{} is not the pool'.format(node))
    txnPoolNodeSet[idx] = restartedNode

    looper.run(checkNodesConnected(txnPoolNodeSet))
    return restartedNode


def changeNodeKeys(looper, stewardClient, stewardWallet, node, verkey):
    nodeNym = hexToFriendly(node.nodestack.verhex)

    op = {
        TXN_TYPE: NODE,
        TARGET_NYM: nodeNym,
        VERKEY: verkey,
        DATA: {
            ALIAS: node.name
        }
    }
    req = stewardWallet.signOp(op)
    stewardClient.submitReqs(req)

    waitForSufficientRepliesForRequests(looper, stewardClient,
                                        requests=[req], fVal=1)

    node.nodestack.clearLocalRoleKeep()
    node.nodestack.clearRemoteRoleKeeps()
    node.nodestack.clearAllDir()
    node.clientstack.clearLocalRoleKeep()
    node.clientstack.clearRemoteRoleKeeps()
    node.clientstack.clearAllDir()


def suspendNode(looper, stewardClient, stewardWallet, nodeNym, nodeName):
    op = {
        TXN_TYPE: NODE,
        TARGET_NYM: nodeNym,
        DATA: {
            SERVICES: [],
            ALIAS: nodeName
        }
    }
    req = stewardWallet.signOp(op)
    stewardClient.submitReqs(req)

    waitForSufficientRepliesForRequests(looper, stewardClient,
                                        requests=[req], fVal=1)

def cancelNodeSuspension(looper, stewardClient, stewardWallet, nodeNym,
                         nodeName):
    op = {
        TXN_TYPE: NODE,
        TARGET_NYM: nodeNym,
        DATA: {
            SERVICES: [VALIDATOR],
            ALIAS: nodeName
        }
    }

    req = stewardWallet.signOp(op)
    stewardClient.submitReqs(req)
    waitForSufficientRepliesForRequests(looper, stewardClient,
                                        requests=[req], fVal=1)


def buildPoolClientAndWallet(clientData, tempDir, clientClass=None,
                             walletClass=None):
    walletClass = walletClass or Wallet
    clientClass = clientClass or TestClient
    name, sigseed = clientData
    w = walletClass(name)
    w.addIdentifier(signer=SimpleSigner(seed=sigseed))
    client, _ = genTestClient(name=name, identifier=w.defaultId,
                              tmpdir=tempDir, usePoolLedger=True,
                              testClientClass=clientClass)
    return client, w


def disconnectPoolNode(poolNodes: Iterable, disconnect: Union[str, TestNode], stopNode=True):
    if isinstance(disconnect, TestNode):
        disconnect = disconnect.name
    assert isinstance(disconnect, str)

    for node in poolNodes:
        if node.name == disconnect and stopNode:
            node.stop()
        else:
            node.nodestack.disconnectByName(disconnect)


def reconnectPoolNode(poolNodes: Iterable, connect: Union[str, TestNode], looper):
    if isinstance(connect, TestNode):
        connect = connect.name
    assert isinstance(connect, str)

    for node in poolNodes:
        if node.name == connect:
            node.start(looper)
        else:
            node.nodestack.reconnectRemoteWithName(connect)


def disconnect_node_and_ensure_disconnected(looper, poolNodes,
                                            disconnect: Union[str, TestNode],
                                            timeout=None,
                                            stopNode=True):
    if isinstance(disconnect, TestNode):
        disconnect = disconnect.name
    assert isinstance(disconnect, str)

    disconnectPoolNode(poolNodes, disconnect, stopNode=stopNode)
    ensure_node_disconnected(looper, disconnect, poolNodes,
                             timeout=timeout)


def reconnect_node_and_ensure_connected(looper, poolNodes,
                                            connect: Union[str, TestNode],
                                            timeout=None):
    if isinstance(connect, TestNode):
        connect = connect.name
    assert isinstance(connect, str)

    reconnectPoolNode(poolNodes, connect, looper)
    looper.run(checkNodesConnected(poolNodes, customTimeout=timeout))
