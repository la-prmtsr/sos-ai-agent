const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("SOSReportRegistry", function () {
  const id = ethers.id("SOS-TEST01");
  const payloadHash = ethers.keccak256(ethers.toUtf8Bytes('{"demo":"payload"}'));
  const planHash = ethers.keccak256(ethers.toUtf8Bytes('{"demo":"plan"}'));

  async function deploy() {
    const [owner, relayer, operator, stranger] = await ethers.getSigners();
    const Registry = await ethers.getContractFactory("SOSReportRegistry");
    const registry = await Registry.deploy(relayer.address, [operator.address]);
    await registry.waitForDeployment();
    return { registry, owner, relayer, operator, stranger };
  }

  it("menjalankan alur lengkap: lapor -> verifikasi -> approve -> sampai", async function () {
    const { registry, relayer, operator } = await deploy();

    await expect(registry.connect(relayer).submitReport(id, payloadHash)).to.emit(registry, "ReportSubmitted");
    await expect(registry.connect(relayer).markVerified(id, planHash, 96))
      .to.emit(registry, "ReportVerified")
      .withArgs(id, planHash, 96);
    await expect(registry.connect(operator).approveDispatch(id, planHash))
      .to.emit(registry, "DispatchApproved")
      .withArgs(id, operator.address, planHash);
    await expect(registry.connect(relayer).confirmDelivery(id)).to.emit(registry, "DeliveryConfirmed");

    const r = await registry.getReport(id);
    expect(r.status).to.equal(4n); // Delivered
    expect(r.payloadHash).to.equal(payloadHash);
    expect(r.decidedBy).to.equal(operator.address);
    expect(r.urgencyScore).to.equal(96n);
    expect(await registry.reportCount()).to.equal(1n);
    expect(await registry.reportIdAt(0)).to.equal(id);
  });

  it("menolak orang yang bukan relayer / operator", async function () {
    const { registry, relayer, stranger } = await deploy();

    await expect(registry.connect(stranger).submitReport(id, payloadHash)).to.be.revertedWithCustomError(
      registry,
      "NotRelayer"
    );
    await registry.connect(relayer).submitReport(id, payloadHash);
    await registry.connect(relayer).markVerified(id, planHash, 50);
    await expect(registry.connect(stranger).approveDispatch(id, planHash)).to.be.revertedWithCustomError(
      registry,
      "NotOperator"
    );
    // backend sendiri pun tidak bisa approve: keputusan tetap di tangan manusia
    await expect(registry.connect(relayer).approveDispatch(id, planHash)).to.be.revertedWithCustomError(
      registry,
      "NotOperator"
    );
  });

  it("menolak approve kalau rencananya beda dari yang tercatat", async function () {
    const { registry, relayer, operator } = await deploy();
    await registry.connect(relayer).submitReport(id, payloadHash);
    await registry.connect(relayer).markVerified(id, planHash, 50);

    const otherPlan = ethers.keccak256(ethers.toUtf8Bytes("rencana lain"));
    await expect(registry.connect(operator).approveDispatch(id, otherPlan)).to.be.revertedWithCustomError(
      registry,
      "PlanMismatch"
    );
  });

  it("menjaga urutan status", async function () {
    const { registry, relayer, operator } = await deploy();

    await registry.connect(relayer).submitReport(id, payloadHash);
    await expect(registry.connect(relayer).submitReport(id, payloadHash)).to.be.revertedWithCustomError(
      registry,
      "ReportExists"
    );
    // belum diverifikasi, belum bisa di-approve
    await expect(registry.connect(operator).approveDispatch(id, planHash)).to.be.revertedWithCustomError(
      registry,
      "WrongStatus"
    );
    // belum di-approve, belum bisa dikonfirmasi sampai
    await expect(registry.connect(relayer).confirmDelivery(id)).to.be.revertedWithCustomError(
      registry,
      "WrongStatus"
    );
  });

  it("operator bisa menolak laporan", async function () {
    const { registry, relayer, operator } = await deploy();
    await registry.connect(relayer).submitReport(id, payloadHash);
    await expect(registry.connect(operator).rejectReport(id)).to.emit(registry, "ReportRejected");
    expect((await registry.getReport(id)).status).to.equal(5n);
    await expect(registry.connect(relayer).markVerified(id, planHash, 10)).to.be.revertedWithCustomError(
      registry,
      "WrongStatus"
    );
  });

  it("owner bisa menambah dan mencabut operator", async function () {
    const { registry, owner, stranger } = await deploy();
    await registry.connect(owner).setOperator(stranger.address, true);
    expect(await registry.isOperator(stranger.address)).to.equal(true);
    await registry.connect(owner).setOperator(stranger.address, false);
    expect(await registry.isOperator(stranger.address)).to.equal(false);
    await expect(registry.connect(stranger).setOperator(stranger.address, true)).to.be.revertedWithCustomError(
      registry,
      "NotOwner"
    );
  });
});
