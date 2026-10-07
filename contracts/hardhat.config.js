require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

const { DEPLOYER_PRIVATE_KEY, BSC_TESTNET_RPC } = process.env;

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      // "paris" = bytecode paling kompatibel di semua chain EVM (tanpa opcode PUSH0)
      evmVersion: "paris",
    },
  },
  networks: {
    // node lokal: jalankan `npm run node` di terminal lain
    localhost: {
      url: "http://127.0.0.1:8545",
    },
    bscTestnet: {
      url: BSC_TESTNET_RPC || "https://bsc-testnet-rpc.publicnode.com",
      chainId: 97,
      accounts: DEPLOYER_PRIVATE_KEY ? [DEPLOYER_PRIVATE_KEY] : [],
    },
  },
};
