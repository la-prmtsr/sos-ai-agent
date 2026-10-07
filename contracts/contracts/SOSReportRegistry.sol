// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title SOSReportRegistry
/// @notice Jejak audit on-chain untuk laporan bencana SOS AI.
///         Data pribadi (lokasi, nama, kondisi medis) TIDAK disimpan di sini.
///         Yang disimpan hanya hash (sidik jari) dari data itu, status laporan,
///         dan siapa yang menyetujui pengiriman bantuan.
contract SOSReportRegistry {
    enum Status {
        None,       // 0 - belum ada
        Reported,   // 1 - laporan masuk dari relawan
        Verified,   // 2 - sudah dicek agent AI, menunggu operator
        Approved,   // 3 - operator menyetujui, bantuan dikirim
        Delivered,  // 4 - relawan konfirmasi bantuan sampai
        Rejected    // 5 - operator menolak
    }

    struct Report {
        bytes32 payloadHash;   // keccak256 dari isi laporan (disimpan off-chain)
        bytes32 planHash;      // keccak256 dari rencana pengiriman buatan agent
        uint64 reportedAt;
        uint64 verifiedAt;
        uint64 decidedAt;
        uint64 deliveredAt;
        uint8 urgencyScore;    // 0-100
        Status status;
        address decidedBy;     // wallet operator yang approve / reject
    }

    address public owner;
    address public relayer; // wallet backend yang menulis laporan atas nama relawan
    mapping(address => bool) public isOperator;

    mapping(bytes32 => Report) private _reports;
    bytes32[] private _reportIds;

    event ReportSubmitted(bytes32 indexed reportId, bytes32 payloadHash, uint64 timestamp);
    event ReportVerified(bytes32 indexed reportId, bytes32 planHash, uint8 urgencyScore);
    event DispatchApproved(bytes32 indexed reportId, address indexed operator, bytes32 planHash);
    event ReportRejected(bytes32 indexed reportId, address indexed operator);
    event DeliveryConfirmed(bytes32 indexed reportId, uint64 timestamp);
    event OperatorSet(address indexed operator, bool allowed);
    event RelayerSet(address indexed relayer);

    error NotOwner();
    error NotRelayer();
    error NotOperator();
    error ReportExists();
    error WrongStatus(Status current);
    error PlanMismatch();
    error InvalidInput();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyRelayer() {
        if (msg.sender != relayer) revert NotRelayer();
        _;
    }

    modifier onlyOperator() {
        if (!isOperator[msg.sender]) revert NotOperator();
        _;
    }

    constructor(address relayer_, address[] memory operators_) {
        if (relayer_ == address(0)) revert InvalidInput();
        owner = msg.sender;
        relayer = relayer_;
        emit RelayerSet(relayer_);
        for (uint256 i = 0; i < operators_.length; i++) {
            isOperator[operators_[i]] = true;
            emit OperatorSet(operators_[i], true);
        }
    }

    // ---------------------------------------------------------------- admin

    function setOperator(address operator, bool allowed) external onlyOwner {
        isOperator[operator] = allowed;
        emit OperatorSet(operator, allowed);
    }

    function setRelayer(address relayer_) external onlyOwner {
        if (relayer_ == address(0)) revert InvalidInput();
        relayer = relayer_;
        emit RelayerSet(relayer_);
    }

    // -------------------------------------------------------------- backend

    /// @notice Langkah 1: backend mencatat laporan baru dari bot Telegram.
    function submitReport(bytes32 reportId, bytes32 payloadHash) external onlyRelayer {
        if (reportId == bytes32(0) || payloadHash == bytes32(0)) revert InvalidInput();
        if (_reports[reportId].status != Status.None) revert ReportExists();

        Report storage r = _reports[reportId];
        r.payloadHash = payloadHash;
        r.reportedAt = uint64(block.timestamp);
        r.status = Status.Reported;
        _reportIds.push(reportId);

        emit ReportSubmitted(reportId, payloadHash, uint64(block.timestamp));
    }

    /// @notice Langkah 2: agent AI selesai, backend mencatat hash rencana pengiriman.
    function markVerified(bytes32 reportId, bytes32 planHash, uint8 urgencyScore) external onlyRelayer {
        Report storage r = _reports[reportId];
        if (r.status != Status.Reported) revert WrongStatus(r.status);
        if (planHash == bytes32(0) || urgencyScore > 100) revert InvalidInput();

        r.planHash = planHash;
        r.urgencyScore = urgencyScore;
        r.verifiedAt = uint64(block.timestamp);
        r.status = Status.Verified;

        emit ReportVerified(reportId, planHash, urgencyScore);
    }

    /// @notice Langkah 4: relawan menekan "bantuan sudah sampai" di Telegram.
    function confirmDelivery(bytes32 reportId) external onlyRelayer {
        Report storage r = _reports[reportId];
        if (r.status != Status.Approved) revert WrongStatus(r.status);

        r.deliveredAt = uint64(block.timestamp);
        r.status = Status.Delivered;

        emit DeliveryConfirmed(reportId, uint64(block.timestamp));
    }

    // ------------------------------------------------------------- operator

    /// @notice Langkah 3: operator menyetujui rencana lewat wallet-nya sendiri.
    /// @dev planHash wajib dikirim ulang supaya operator menyetujui persis
    ///      rencana yang dia lihat di layar, bukan rencana yang diganti diam-diam.
    function approveDispatch(bytes32 reportId, bytes32 planHash) external onlyOperator {
        Report storage r = _reports[reportId];
        if (r.status != Status.Verified) revert WrongStatus(r.status);
        if (r.planHash != planHash) revert PlanMismatch();

        r.status = Status.Approved;
        r.decidedBy = msg.sender;
        r.decidedAt = uint64(block.timestamp);

        emit DispatchApproved(reportId, msg.sender, planHash);
    }

    function rejectReport(bytes32 reportId) external onlyOperator {
        Report storage r = _reports[reportId];
        if (r.status != Status.Reported && r.status != Status.Verified) revert WrongStatus(r.status);

        r.status = Status.Rejected;
        r.decidedBy = msg.sender;
        r.decidedAt = uint64(block.timestamp);

        emit ReportRejected(reportId, msg.sender);
    }

    // ----------------------------------------------------------------- view

    function getReport(bytes32 reportId) external view returns (Report memory) {
        return _reports[reportId];
    }

    function reportCount() external view returns (uint256) {
        return _reportIds.length;
    }

    function reportIdAt(uint256 index) external view returns (bytes32) {
        return _reportIds[index];
    }
}
