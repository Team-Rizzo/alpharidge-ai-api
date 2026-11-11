"""Tie blocks read from block bodies when a node no longer holds their state."""

from types import SimpleNamespace

from app.utils import block

# Timestamp.set(1794375744000): length 10, version 5, pallet 2, call 0, compact u64.
SET = "0x280502000b00fa20c9a101"


def compact_len(n: int) -> bytes:
    """`n` as a SCALE compact (lengths under 2**30)."""
    if n < 1 << 6:
        return bytes([n << 2])
    if n < 1 << 14:
        return ((n << 2) | 1).to_bytes(2, "little")
    return ((n << 2) | 2).to_bytes(4, "little")


def test_compact_modes():
    assert block.compact(bytes([0x04]), 0) == (1, 1)
    assert block.compact(bytes([0x15, 0x01]), 0) == (69, 2)
    assert block.compact(bytes([0x02, 0x00, 0x01, 0x00]), 0) == (16384, 4)
    assert block.compact(bytes.fromhex("0b00fa20c9a101"), 0) == (1794375744000, 7)


def test_inherent_calls_skip_signed_extrinsics():
    signed = "0x" + "1c" + "84" + "00" * 6
    assert block.inherent_calls([SET, signed]) == [(2, 0, 1794375744000)]


class FakeChain:
    """Five blocks 12 s apart; state is kept only for the newest two, and older state
    reads answer 0."""

    def __init__(self):
        self.stamps = {n: 1_000_000 + 12_000 * n for n in range(1, 6)}

    def get_chain_finalised_head(self):
        return "0x5"

    def get_block_number(self, h):
        return int(h[2:])

    def get_block_hash(self, n):
        return f"0x{n}"

    def query(self, module, name, block_hash):
        n = int(block_hash[2:])
        return SimpleNamespace(value=self.stamps[n] if n >= 4 else 0)

    def rpc_request(self, method, params):
        n = int(params[0][2:])
        v = self.stamps[n]
        body = bytes([0x05, 0x02, 0x00, 0x13]) + v.to_bytes(4 + 4, "little")[:8]
        xt = compact_len(len(body)) + body
        return {"result": {"block": {"extrinsics": ["0x" + xt.hex()]}}}


def test_old_tie_blocks_come_from_block_bodies(monkeypatch):
    chain = FakeChain()
    monkeypatch.setattr(block, "is_mock", lambda: False)
    monkeypatch.setattr(block, "_tie_substrate", lambda: chain)
    monkeypatch.setattr(block, "_stamps", {})
    monkeypatch.setattr(block, "_tie_head", (0, 0.0))
    monkeypatch.setattr(block, "_set_call", None)
    assert block.get_tie_block(chain.stamps[2] - 5) == (2, "0x2")
    assert block.get_tie_block(chain.stamps[2]) == (2, "0x2")
    assert block._set_call == (2, 0)
