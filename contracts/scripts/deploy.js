// Deploy SOSReportRegistry.
//   lokal : npm run deploy:local   (butuh `npm run node` jalan di terminal lain)
//   BSC   : npm run deploy:bsc
const fs = require("fs");
const path = require("path");
const hre = require("hardhat");

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  if (!deployer) {
    throw new Error("Tidak ada wallet deployer. Isi DEPLOYER_PRIVATE_KEY di contracts/.env");
  }

  const relayer = process.env.RELAYER_ADDRESS || deployer.address;
  const operators = (process.env.OPERATOR_ADDRESSES || deployer.address)
    .split(",")
    .map((a) => a.trim())
    .filter(Boolean);

  const network = await hre.ethers.provider.getNetwork();
  console.log(`Network  : ${hre.network.name} (chainId ${network.chainId})`);
  console.log(`Deployer : ${deployer.address}`);
  console.log(`Relayer  : ${relayer}`);
  console.log(`Operator : ${operators.join(", ")}`);

  const Registry = await hre.ethers.getContractFactory("SOSReportRegistry");
  const registry = await Registry.deploy(relayer, operators);
  await registry.waitForDeployment();
  const address = await registry.getAddress();

  // simpan catatan deployment
  const outDir = path.join(__dirname, "..", "deployments");
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(
    path.join(outDir, `${hre.network.name}.json`),
    JSON.stringify(
      { address, chainId: Number(network.chainId), deployer: deployer.address, relayer, operators },
      null,
      2
    )
  );

  // salin ABI ke backend supaya backend & dashboard selalu pakai ABI terbaru
  const artifact = await hre.artifacts.readArtifact("SOSReportRegistry");
  const abiPath = path.join(__dirname, "..", "..", "backend", "app", "abi.json");
  if (fs.existsSync(path.dirname(abiPath))) {
    fs.writeFileSync(abiPath, JSON.stringify(artifact.abi, null, 2));
  }

  console.log("\nContract ter-deploy di:", address);
  console.log("\nSalin baris ini ke backend/.env :");
  console.log(`CONTRACT_ADDRESS=${address}`);
  console.log(`CHAIN_ID=${network.chainId}`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
