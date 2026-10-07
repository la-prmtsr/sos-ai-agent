"""Jembatan backend -> smart contract SOSReportRegistry.

Backend hanya bisa: mencatat laporan, mencatat hasil verifikasi, mencatat bantuan sampai.
Backend TIDAK bisa approve: itu hanya bisa dilakukan wallet operator dari dashboard.

Semua fungsi di sini sinkron (blocking). Panggil lewat asyncio.to_thread(...) dari kode async.
"""
import threading
from typing import Any, Optional

from web3 import Web3

from . import config

STATUS_NAMES = ["NONE", "REPORTED", "VERIFIED", "APPROVED", "DELIVERED", "REJECTED"]

_lock = threading.Lock()  # satu transaksi dalam satu waktu, supaya nonce tidak bentrok
_state: dict[str, Any] = {}


def keccak_text(text: str) -> str:
    """Hash keccak256 dari teks (UTF-8), hasilnya '0x...' 64 digit hex."""
    return Web3.to_hex(Web3.keccak(text=text))


def report_id(code: str) -> str:
    """ID laporan on-chain (bytes32) = keccak256 dari kode laporan."""
    return keccak_text(code)


def _setup() -> tuple[Web3, Any, Any]:
    if not config.CHAIN_ENABLED:
        raise RuntimeError("Blockchain belum diatur. Isi CONTRACT_ADDRESS dan RELAYER_PRIVATE_KEY di backend/.env")
    if not _state:
        w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 20}))
        _state["w3"] = w3
        _state["account"] = w3.eth.account.from_key(config.RELAYER_PRIVATE_KEY)
        _state["contract"] = w3.eth.contract(
            address=Web3.to_checksum_address(config.CONTRACT_ADDRESS), abi=config.CONTRACT_ABI
        )
    return _state["w3"], _state["contract"], _state["account"]


def relayer_address() -> Optional[str]:
    if not config.CHAIN_ENABLED:
        return None
    return _setup()[2].address


def _b32(hex_value: str) -> bytes:
    return Web3.to_bytes(hexstr=hex_value)


def _send(fn: Any) -> str:
    """Tanda tangani + kirim transaksi, tunggu sampai masuk blok, kembalikan tx hash."""
    w3, _, account = _setup()
    with _lock:
        tx = fn.build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address, "pending"),
                "chainId": config.CHAIN_ID,
                "gasPrice": w3.eth.gas_price,
            }
        )
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        tx_hash = w3.eth.send_raw_transaction(raw)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError(f"Transaksi gagal (reverted): {Web3.to_hex(tx_hash)}")
    return Web3.to_hex(tx_hash)


def submit_report(code: str, payload_hash: str) -> str:
    _, contract, _ = _setup()
    return _send(contract.functions.submitReport(_b32(report_id(code)), _b32(payload_hash)))


def mark_verified(code: str, plan_hash: str, urgency: int) -> str:
    _, contract, _ = _setup()
    return _send(contract.functions.markVerified(_b32(report_id(code)), _b32(plan_hash), int(urgency)))


def confirm_delivery(code: str) -> str:
    _, contract, _ = _setup()
    return _send(contract.functions.confirmDelivery(_b32(report_id(code))))


def owner() -> str:
    """Alamat pemilik contract (yang berhak menambah dan mencabut operator)."""
    _, contract, _ = _setup()
    return contract.functions.owner().call()


def is_operator(address: str) -> bool:
    """Apakah alamat ini operator menurut contract?"""
    _, contract, _ = _setup()
    return bool(contract.functions.isOperator(Web3.to_checksum_address(address)).call())


def get_report(code: str) -> dict[str, Any]:
    """Baca status laporan langsung dari contract (gratis, tanpa transaksi)."""
    _, contract, _ = _setup()
    r = contract.functions.getReport(_b32(report_id(code))).call()
    return {
        "payload_hash": Web3.to_hex(r[0]),
        "plan_hash": Web3.to_hex(r[1]),
        "reported_at": r[2],
        "verified_at": r[3],
        "decided_at": r[4],
        "delivered_at": r[5],
        "urgency": r[6],
        "status": STATUS_NAMES[r[7]],
        "decided_by": r[8],
    }
