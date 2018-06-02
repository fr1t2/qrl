# coding=utf-8
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.
from typing import Optional

from pyqrllib.pyqrllib import bin2hstr
from pyqryptonight.pyqryptonight import StringToUInt256, UInt256ToString

from qrl.core import config
from qrl.core.AddressState import AddressState
from qrl.core.Block import Block
from qrl.core.BlockMetadata import BlockMetadata
from qrl.core.DifficultyTracker import DifficultyTracker
from qrl.core.GenesisBlock import GenesisBlock
from qrl.core.Transaction import Transaction, CoinBase
from qrl.core.TransactionPool import TransactionPool
from qrl.core.misc import logger
from qrl.generated import qrl_pb2


class ChainManager:
    def __init__(self, state):
        self.state = state
        self.tx_pool = TransactionPool(None)
        self.last_block = Block.deserialize(GenesisBlock().serialize())
        self.current_difficulty = StringToUInt256(str(config.dev.genesis_difficulty))

        self.trigger_miner = False

    @property
    def height(self):
        return self.last_block.block_number

    def set_broadcast_tx(self, broadcast_tx):
        self.tx_pool.set_broadcast_tx(broadcast_tx)

    def get_last_block(self) -> Block:
        return self.last_block

    def get_cumulative_difficulty(self):
        last_block_metadata = self.state.get_block_metadata(self.last_block.headerhash)
        return last_block_metadata.cumulative_difficulty

    def load(self, genesis_block):
        height = self.state.get_mainchain_height()

        if height == -1:
            self.state.put_block(genesis_block, None)
            block_number_mapping = qrl_pb2.BlockNumberMapping(headerhash=genesis_block.headerhash,
                                                              prev_headerhash=genesis_block.prev_headerhash)

            self.state.put_block_number_mapping(genesis_block.block_number, block_number_mapping, None)
            parent_difficulty = StringToUInt256(str(config.dev.genesis_difficulty))

            self.current_difficulty, _ = DifficultyTracker.get(
                measurement=config.dev.mining_setpoint_blocktime,
                parent_difficulty=parent_difficulty)

            block_metadata = BlockMetadata.create()
            block_metadata.set_block_difficulty(self.current_difficulty)
            block_metadata.set_cumulative_difficulty(self.current_difficulty)

            self.state.put_block_metadata(genesis_block.headerhash, block_metadata, None)
            addresses_state = dict()
            for genesis_balance in GenesisBlock().genesis_balance:
                bytes_addr = genesis_balance.address
                addresses_state[bytes_addr] = AddressState.get_default(bytes_addr)
                addresses_state[bytes_addr]._data.balance = genesis_balance.balance

            for tx_idx in range(1, len(genesis_block.transactions)):
                tx = Transaction.from_pbdata(genesis_block.transactions[tx_idx])
                for addr in tx.addrs_to:
                    addresses_state[addr] = AddressState.get_default(addr)

            coinbase_tx = Transaction.from_pbdata(genesis_block.transactions[0])

            if not isinstance(coinbase_tx, CoinBase):
                return False

            addresses_state[coinbase_tx.addr_to] = AddressState.get_default(coinbase_tx.addr_to)

            if not coinbase_tx.validate_extended():
                return False

            coinbase_tx.apply_state_changes(addresses_state)

            for tx_idx in range(1, len(genesis_block.transactions)):
                tx = Transaction.from_pbdata(genesis_block.transactions[tx_idx])
                tx.apply_state_changes(addresses_state)

            self.state.put_addresses_state(addresses_state)
            self.state.update_tx_metadata(genesis_block, None)
            self.state.update_mainchain_height(0, None)
        else:
            self.last_block = self.get_block_by_number(height)
            self.current_difficulty = self.state.get_block_metadata(self.last_block.headerhash).block_difficulty

    def _apply_block(self, block: Block, batch=None) -> bool:
        address_set = self.state.prepare_address_list(block)  # Prepare list for current block
        addresses_state = self.state.get_state_mainchain(address_set)
        if not block.apply_state_changes(addresses_state):
            return False
        self.state.put_addresses_state(addresses_state, batch)
        return True

    def _update_chainstate(self, block: Block, batch=None):
        self.last_block = block
        self._update_mainchain(block, batch)
        self.tx_pool.remove_tx_in_block_from_pool(block)
        self.state.update_mainchain_height(block.block_number, batch)
        self.state.update_tx_metadata(block, batch)

    def _try_branch_add_block(self, block, batch=None) -> bool:
        if self.last_block.headerhash == block.prev_headerhash:
            if not self._apply_block(block):
                return False

        self.state.put_block(block, None)
        self.add_block_metadata(block.headerhash, block.timestamp, block.prev_headerhash, None)

        last_block_metadata = self.state.get_block_metadata(self.last_block.headerhash)
        new_block_metadata = self.state.get_block_metadata(block.headerhash)
        last_block_difficulty = int(UInt256ToString(last_block_metadata.cumulative_difficulty))
        new_block_difficulty = int(UInt256ToString(new_block_metadata.cumulative_difficulty))

        if new_block_difficulty > last_block_difficulty:
            if self.last_block.headerhash != block.prev_headerhash:
                self.fork_recovery(block)

            self._update_chainstate(block, batch)
            self.tx_pool.check_stale_txn(block.block_number)
            self.trigger_miner = True

        return True

    def remove_block_from_mainchain(self, block: Block, latest_block_number: int, batch):
        addresses_set = self.state.prepare_address_list(block)
        addresses_state = self.state.get_state_mainchain(addresses_set)
        for tx_idx in range(len(block.transactions) - 1, -1, -1):
            tx = Transaction.from_pbdata(block.transactions[tx_idx])
            tx.revert_state_changes(addresses_state, self.state)

        self.tx_pool.add_tx_from_block_to_pool(block, latest_block_number)
        self.state.update_mainchain_height(block.block_number - 1, batch)
        self.state.rollback_tx_metadata(block, batch)
        self.state.remove_blocknumber_mapping(block.block_number, batch)
        self.state.put_addresses_state(addresses_state, batch)

    def get_fork_point(self, header_hash: bytes):
        forked_header_hash = header_hash

        hash_path = []
        while True:
            block = self.state.get_block(forked_header_hash)
            if not block:
                raise Exception('[get_state] No Block Found %s, Initiator %s', forked_header_hash, header_hash)
            mainchain_block = self.get_block_by_number(block.block_number)
            if mainchain_block and mainchain_block.headerhash == block.headerhash:
                break
            if block.block_number == 0:
                raise Exception('[get_state] Alternate chain genesis is different, Initiator %s', forked_header_hash)
            hash_path.append(forked_header_hash)
            forked_header_hash = block.prev_headerhash

        return forked_header_hash, hash_path

    def rollback(self, forked_header_hash: bytes):
        """
        Rollback from last block to the block just before the forked_header_hash
        :param forked_header_hash:
        :return:
        """
        hash_path = []
        while self.last_block.headerhash != forked_header_hash:
            block = self.state.get_block(self.last_block.headerhash)
            mainchain_block = self.get_block_by_number(block.block_number)
            if block.headerhash == mainchain_block.headerhash:
                break
            hash_path.append(self.last_block.headerhash)
            self.remove_block_from_mainchain(self.last_block, block.block_number, None)
            self.last_block = self.state.get_block(self.last_block.prev_headerhash)

        return hash_path

    def add_chain(self, hash_path, batch=None):
        """
        Add series of blocks whose headerhash mentioned into hash_path
        :param hash_path:
        :param batch:
        :return:
        """
        for header_hash in hash_path:
            block = self.state.get_block(header_hash)
            if not self._apply_block(block, batch):
                return header_hash

            self._update_chainstate(block, batch)

        return None

    def fork_recovery(self, block: Block):
        forked_header_hash, hash_path = self.get_fork_point(block.headerhash)
        old_hash_path = self.rollback(forked_header_hash)

        if self.add_chain(hash_path[-1::-1]):
            # If above condition is true, then it means, the node failed to add_chain
            # Thus old chain state, must be retrieved
            self.rollback(forked_header_hash)
            self.add_chain(old_hash_path[-1::-1])  # Restores the old chain state
            return

        self.trigger_miner = True

    def _add_block(self, block, batch=None):
        self.trigger_miner = False

        block_size_limit = self.state.get_block_size_limit(block)
        if block_size_limit and block.size > block_size_limit:
            logger.info('Block Size greater than threshold limit %s > %s', block.size, block_size_limit)
            return False

        if self._try_branch_add_block(block, batch):
            return True

        return False

    def add_block(self, block: Block) -> bool:
        if block.block_number < self.height - config.dev.reorg_limit:
            logger.debug('Skipping block #%s as beyond re-org limit', block.block_number)
            return False

        batch = self.state.get_batch()
        if self._add_block(block, batch=batch):
            self.state.write_batch(batch)
            logger.info('Added Block #%s %s', block.block_number, bin2hstr(block.headerhash))
            return True

        return False

    def add_block_metadata(self,
                           headerhash,
                           block_timestamp,
                           parent_headerhash,
                           batch):
        block_metadata = self.state.get_block_metadata(headerhash)
        if not block_metadata:
            block_metadata = BlockMetadata.create()

        parent_metadata = self.state.get_block_metadata(parent_headerhash)

        parent_block_difficulty = parent_metadata.block_difficulty
        parent_cumulative_difficulty = parent_metadata.cumulative_difficulty

        block_metadata.update_last_headerhashes(parent_metadata.last_N_headerhashes, parent_headerhash)
        measurement = self.state.get_measurement(block_timestamp, parent_headerhash, parent_metadata)

        block_difficulty, _ = DifficultyTracker.get(
            measurement=measurement,
            parent_difficulty=parent_block_difficulty)

        block_cumulative_difficulty = StringToUInt256(str(
            int(UInt256ToString(block_difficulty)) +
            int(UInt256ToString(parent_cumulative_difficulty))))

        block_metadata.set_block_difficulty(block_difficulty)
        block_metadata.set_cumulative_difficulty(block_cumulative_difficulty)

        parent_metadata.add_child_headerhash(headerhash)
        self.state.put_block_metadata(parent_headerhash, parent_metadata, batch)
        self.state.put_block_metadata(headerhash, block_metadata, batch)

        # Call once to populate the cache
        self.state.get_block_datapoint(headerhash)

    def _update_mainchain(self, block, batch):
        block_number_mapping = None
        while block_number_mapping is None or block.headerhash != block_number_mapping.headerhash:
            block_number_mapping = qrl_pb2.BlockNumberMapping(headerhash=block.headerhash,
                                                              prev_headerhash=block.prev_headerhash)
            self.state.put_block_number_mapping(block.block_number, block_number_mapping, batch)
            block = self.state.get_block(block.prev_headerhash)
            block_number_mapping = self.state.get_block_number_mapping(block.block_number)

    def get_block_by_number(self, block_number) -> Optional[Block]:
        return self.state.get_block_by_number(block_number)

    def get_address(self, address):
        return self.state.get_address_state(address)

    def get_transaction(self, transaction_hash) -> list:
        return self.state.get_tx_metadata(transaction_hash)

    def get_unconfirmed_transaction(self, transaction_hash) -> list:
        for tx_set in self.tx_pool.transactions:
            tx = tx_set[1].transaction
            if tx.txhash == transaction_hash:
                return [tx, tx_set[1].timestamp]
        return []

    def get_headerhashes(self, start_blocknumber):
        start_blocknumber = max(0, start_blocknumber)
        end_blocknumber = min(self.last_block.block_number,
                              start_blocknumber + 2 * config.dev.reorg_limit)

        total_expected_headerhash = end_blocknumber - start_blocknumber + 1

        node_header_hash = qrl_pb2.NodeHeaderHash()
        node_header_hash.block_number = start_blocknumber

        block = self.state.get_block_by_number(end_blocknumber)
        block_headerhash = block.headerhash
        node_header_hash.headerhashes.append(block_headerhash)
        end_blocknumber -= 1

        while end_blocknumber >= start_blocknumber:
            block_metadata = self.state.get_block_metadata(block_headerhash)
            for headerhash in block_metadata.last_N_headerhashes[-1::-1]:
                node_header_hash.headerhashes.append(headerhash)
            end_blocknumber -= len(block_metadata.last_N_headerhashes)
            if len(block_metadata.last_N_headerhashes) == 0:
                break
            block_headerhash = block_metadata.last_N_headerhashes[0]

        node_header_hash.headerhashes[:] = node_header_hash.headerhashes[-1::-1]
        del node_header_hash.headerhashes[:len(node_header_hash.headerhashes) - total_expected_headerhash]

        return node_header_hash
