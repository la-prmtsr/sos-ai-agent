# SOS AI

Sistem bantuan bencana: relawan melapor lewat bot Telegram, rangkaian agent AI
memeriksa laporan dan menyusun rencana pengiriman, operator menyetujui rencana itu
dengan wallet, dan setiap langkah punya jejak di blockchain.

Panduan ini ditulis untuk orang yang belum pernah menyentuh web3. Ikuti tahap A
sampai D berurutan. Setiap tahap menghasilkan sesuatu yang bisa dicoba, jadi kalau
waktu habis di tengah, kamu tetap punya demo yang jalan.

## Isi folder

```
sos-ai/
├── contracts/     Smart contract (Solidity + Hardhat)
│   ├── contracts/SOSReportRegistry.sol   contract-nya
│   ├── scripts/deploy.js                 script deploy
│   └── test/                             6 unit test
├── backend/       Python: API + bot Telegram + agent, jalan sebagai SATU proses
│   └── app/
│       ├── main.py          titik masuk (FastAPI)
│       ├── telegram_bot.py  formulir laporan dengan tombol
│       ├── agents.py        pipeline agent (LangGraph StateGraph)
│       ├── tools.py         tool agent: gempa USGS, cuaca, rute, LLM
│       ├── service.py       alur: lapor -> chain -> agent -> operator -> notifikasi
│       ├── chain.py         jembatan ke smart contract (web3.py)
│       ├── db.py            database SQLite
│       └── config.py        membaca .env
└── dashboard/     Halaman operator (HTML + JS biasa, ethers.js, Leaflet)
```

## Cara kerjanya

1. Relawan mengisi formulir di Telegram: lokasi, jumlah pengungsi, kebutuhan, akses jalan, korban kritis.
2. Backend menyimpan laporan di database, lalu mencatat **hash** laporan ke contract (`submitReport`).
3. Lima node agent berjalan berurutan: triage, verifikasi, alokasi, armada, supervisor.
   Hasilnya satu kartu rekomendasi. Hash kartu itu dicatat ke contract (`markVerified`).
4. Operator membuka dashboard, menghubungkan wallet, lalu menekan **Setujui dan kirim bantuan**.
   Itu transaksi `approveDispatch` yang ditandatangani wallet operator sendiri.
5. Backend melihat status on-chain berubah, lalu mengabari relawan di Telegram.
6. Relawan menekan **Bantuan sudah sampai**, backend mencatat `confirmDelivery`.

Yang ada di blockchain hanya hash, status, skor urgensi, dan alamat wallet operator.
Lokasi dan data pengungsi tetap di database, karena data di blockchain publik dan permanen.

## Yang harus di-install

| Alat | Untuk apa | Catatan |
|---|---|---|
| Node.js 20 atau 22 (LTS) | Hardhat (compile, test, deploy contract) | nodejs.org |
| Python 3.11, 3.12, atau 3.13 | backend | python.org. Di Windows centang "Add Python to PATH" |
| VS Code | editor | bebas pakai editor lain |
| MetaMask | wallet operator | ekstensi browser Chrome/Brave/Firefox |
| Telegram | membuat dan mencoba bot | lebih enak di HP karena ada tombol kirim lokasi |

"Environment"-nya adalah laptop kamu sendiri. Tidak perlu server atau domain: bot
memakai long polling, jadi cukup jalan di laptop yang tersambung internet.
Semua rahasia (token, private key) disimpan di file `.env`, satu di `backend/` dan satu di `contracts/`.

---

## Tahap A: jalankan tanpa blockchain (sekitar 10 menit)

Tujuannya memastikan Python dan dashboard jalan dulu.

Buka terminal di folder `sos-ai/backend`.

**Windows (Command Prompt):**
```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --port 8000
```

**macOS / Linux:**
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app.main:app --port 8000
```

Buka http://localhost:8000 lalu klik **Buat laporan contoh**. Kamu akan melihat
laporan masuk antrean, kartu rekomendasi, dan log agent. Status di pojok kanan atas
bertuliskan "Mode tanpa chain": tombol setujui jalan, tapi belum ada bukti on-chain.

Menghentikan server: `Ctrl + C`. Setiap kali mengubah `.env`, hentikan lalu jalankan lagi.

## Tahap B: bot Telegram (sekitar 10 menit)

1. Di Telegram, cari **@BotFather**, kirim `/newbot`, ikuti pertanyaannya (nama bot, lalu username yang berakhiran `bot`).
2. BotFather memberi token seperti `1234567890:AAH...`. Tempel ke `backend/.env`:
   ```
   TELEGRAM_BOT_TOKEN=1234567890:AAH...
   ```
3. Restart backend. Di log harus muncul `Bot Telegram: AKTIF`.
4. Buka bot kamu di Telegram, kirim `/lapor`, isi formulirnya.
5. Kirim `/id` ke bot untuk melihat ID Telegram kamu. Masukkan ke `REGISTERED_VOLUNTEERS`
   supaya kamu dihitung sebagai relawan terdaftar (trust score 0.95, bukan 0.60).

Perintah bot: `/lapor` mulai laporan, `/batal` membatalkan, `/id` melihat ID.

Telegram versi laptop tidak bisa mengirim lokasi. Bot menerima koordinat yang diketik,
contoh `-6.7123, 106.8451`.

Satu token hanya boleh dipakai satu backend yang sedang jalan. Kalau dua anggota tim
menjalankan backend dengan token yang sama, bot akan error "Conflict". Saat development,
tiap orang bikin bot sendiri.

## Tahap C: blockchain lokal (sekitar 20 menit)

Hardhat menyediakan blockchain tiruan di laptop. Gratis, instan, tidak butuh faucet.
Pakai ini untuk development, baru pindah ke testnet di tahap D.

Kamu butuh **tiga terminal**.

**Terminal 1, di `sos-ai/contracts`:**
```
npm install
npm test
npm run node
```
`npm test` harus menampilkan `6 passing`. `npm run node` menyalakan blockchain lokal dan
mencetak 20 akun beserta private key-nya. Biarkan terminal ini terbuka.

**Terminal 2, di `sos-ai/contracts`:**
```
npm run deploy:local
```
Catat alamat contract yang dicetak (`CONTRACT_ADDRESS=0x...`).

**Isi `backend/.env`:**
```
RPC_URL=http://127.0.0.1:8545
CHAIN_ID=31337
CHAIN_NAME=Hardhat Lokal
CONTRACT_ADDRESS=0x...alamat dari terminal 2...
RELAYER_PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
```
Private key di atas adalah "Account #0" bawaan Hardhat. Key itu diketahui semua orang,
jadi hanya boleh dipakai di blockchain lokal.

**Terminal 3, di `sos-ai/backend`:** hapus database lama lalu jalankan backend.
```
del sos.db          (macOS/Linux: rm -f sos.db)
python -m uvicorn app.main:app --port 8000
```

**Siapkan MetaMask:**
1. Tambah jaringan secara manual: nama `Hardhat Lokal`, RPC URL `http://127.0.0.1:8545`,
   Chain ID `31337`, simbol `ETH`.
2. Import akun: pilih "Import account", tempel private key Account #0 di atas.

**Coba:** buka http://localhost:8000, buat laporan (lewat bot atau tombol contoh).
Panel "Bukti on-chain" harus menampilkan tiga centang hijau. Klik **Hubungkan wallet**,
lalu **Setujui dan kirim bantuan**, konfirmasi di MetaMask. Kalau laporannya dari
Telegram, relawan langsung mendapat pesan.

Setiap kali `npm run node` dimatikan, blockchain lokal kembali kosong. Urutannya:
nyalakan node, deploy ulang, hapus `sos.db`, restart backend. Di MetaMask buka
Settings > Advanced > "Clear activity tab data" supaya nomor urut transaksinya ikut reset.

## Tahap D: BSC testnet (sekitar 30 menit)

Sekarang contract-nya dipasang di jaringan publik, jadi juri bisa memeriksa transaksinya
di explorer.

1. **Buat akun MetaMask baru khusus testnet.** Jangan pakai akun yang berisi uang asli,
   karena private key-nya akan kamu taruh di file `.env`.
2. **Minta tBNB gratis** di faucet resmi: https://www.bnbchain.org/en/testnet-faucet
   (masukkan alamat wallet tadi). Butuh sedikit saja; satu laporan menghabiskan gas yang sangat kecil.
3. **Ambil private key** akun itu: MetaMask > Account details > Show private key.
4. Di folder `contracts`, salin `.env.example` menjadi `.env`, lalu isi:
   ```
   DEPLOYER_PRIVATE_KEY=0x...private key akun testnet...
   ```
5. Deploy:
   ```
   npm run deploy:bsc
   ```
6. **Ubah `backend/.env`:**
   ```
   RPC_URL=https://bsc-testnet-rpc.publicnode.com
   CHAIN_ID=97
   CHAIN_NAME=BSC Testnet
   EXPLORER_URL=https://testnet.bscscan.com
   CONTRACT_ADDRESS=0x...alamat hasil deploy...
   RELAYER_PRIVATE_KEY=0x...private key yang sama...
   ```
7. Hapus `sos.db`, restart backend, buka dashboard. Saat klik **Hubungkan wallet**,
   MetaMask akan menawarkan menambah jaringan BSC Testnet secara otomatis.

Sekarang setiap hash transaksi di dashboard bisa diklik dan terbuka di explorer.

**Memisahkan peran (disarankan untuk demo).** Secara default satu wallet menjadi pemilik,
relayer (backend), dan operator sekaligus. Supaya klaim "keputusan di tangan manusia"
lebih kuat, jadikan wallet anggota tim lain sebagai operator. Sebelum deploy, isi di `contracts/.env`:
```
OPERATOR_ADDRESSES=0x...alamat MetaMask si operator...
```
Dengan begitu backend tidak bisa menyetujui laporannya sendiri. Contract memang menolaknya,
dan itu salah satu yang diuji di `npm test`.

Kalau hackathon kamu memakai chain EVM lain, cukup ganti `RPC_URL`, `CHAIN_ID`,
`CHAIN_NAME`, `EXPLORER_URL`, dan tambahkan jaringannya di `contracts/hardhat.config.js`.

## Tahap E: LLM (opsional)

Tanpa LLM semuanya tetap jalan; alasan rekomendasi ditulis dari template. Kalau diisi,
Supervisor Agent meminta LLM menulis alasan 2-3 kalimat dari fakta hasil agent lain.
Bisa memakai provider apa pun yang formatnya kompatibel dengan OpenAI:
```
LLM_API_KEY=...
LLM_BASE_URL=...   (lihat contoh di .env.example, cek dokumentasi provider)
LLM_MODEL=...      (nama model dari provider)
```

---

## Apa yang asli dan apa yang masih sederhana

Bagian ini penting untuk pitch. Juri hampir selalu bertanya, dan jawaban jujur lebih kuat
daripada klaim yang tidak bisa dibuktikan.

**Asli dan bisa didemokan:**
- Formulir Telegram, pipeline LangGraph (termasuk cabang untuk laporan tidak valid), dan log agent di dashboard adalah keluaran sungguhan.
- Data gempa (USGS), cuaca (Open-Meteo), rute dan waktu tempuh (OSRM), nama tempat (OpenStreetMap) diambil langsung saat laporan diproses.
- Hash laporan dan rencana benar-benar di blockchain, dan dashboard menghitung ulang hash-nya di browser untuk membuktikan datanya tidak diubah.
- Persetujuan operator adalah transaksi wallet. Backend tidak punya hak menyetujui.

**Masih sederhana (sebut sebagai "future work"):**
- Kondisi jalan adalah klaim relawan. Belum ada verifikasi dari citra satelit, dan sistem menandainya begitu di layar. Jangan menulis "diverifikasi satelit".
- Isi bantuan, skor urgensi, dan pemilihan armada dihitung dengan rumus tetap, bukan model. Rumusnya ada di `agents.py` dan rincian skornya tampil di log.
- Stok gudang, daftar armada, dan data kependudukan belum ada.
- API backend belum punya login. Siapa pun yang bisa membuka URL-nya bisa melihat lokasi laporan. Untuk demo lokal tidak masalah; sebelum di-hosting publik, tambahkan autentikasi dan set `ALLOW_SIMULATE=false`.

## Urutan demo 3 menit

1. Tunjukkan dashboard kosong dan status sistem di pojok kanan atas.
2. Di HP, kirim `/lapor` ke bot, isi formulir (sekitar 20 detik).
3. Laporan muncul di antrean. Buka log agent, tunjukkan tool yang dipanggil.
4. Tunjukkan panel "Bukti on-chain": tiga centang, klik hash transaksi, buka explorer.
5. Klik **Setujui dan kirim bantuan**, tanda tangani di MetaMask.
6. HP menerima notifikasi. Tekan **Bantuan sudah sampai**. Status di dashboard berubah.

## Kalau ada masalah

| Gejala | Penyebab dan solusi |
|---|---|
| `python`/`py` tidak dikenali | Python belum masuk PATH. Install ulang dan centang "Add Python to PATH". |
| PowerShell menolak `activate` | Pakai Command Prompt, atau jalankan `Set-ExecutionPolicy -Scope Process Bypass` dulu. |
| Bot tidak membalas | Token salah, atau backend belum di-restart setelah `.env` diubah. Cek log: harus ada `Bot Telegram: AKTIF`. |
| Log bot: `Conflict: terminated by other getUpdates` | Token yang sama dipakai di dua tempat. Matikan salah satunya. |
| Panel bukti: "belum tercatat di contract" | Saldo gas wallet backend habis, atau node lokal baru di-restart. Lihat baris `[CHAIN]` di log agent, perbaiki, lalu klik "Catat ulang ke blockchain". |
| Tombol setujui mati, tertulis "bukan operator" | Wallet MetaMask yang terhubung bukan operator. Pakai wallet yang didaftarkan saat deploy. |
| MetaMask: nonce too high / transaksi macet (lokal) | Settings > Advanced > Clear activity tab data. |
| `npm run deploy:bsc` gagal: insufficient funds | Wallet deployer belum punya tBNB. Minta dari faucet. |
| `npm run deploy:bsc`: "Tidak ada wallet deployer" | `contracts/.env` belum dibuat atau `DEPLOYER_PRIVATE_KEY` kosong. |
| Log agent: "USGS/Open-Meteo tidak bisa dihubungi" | Laptop tidak tersambung internet atau layanannya sedang lambat. Agent tetap jalan dan mencatat datanya tidak tersedia. |
| Peta abu-abu | Tile peta butuh internet. Coba ganti ke "Peta jalan" di pojok kanan atas peta. |

## Menaruh di GitHub

File `.gitignore` sudah mengecualikan `.env`, `node_modules`, dan `sos.db`. Sebelum
`git push`, jalankan `git status` dan pastikan tidak ada file `.env` yang ikut.
Kalau private key atau token bot pernah ter-upload, anggap bocor: buat wallet baru
dan minta token baru ke BotFather (`/revoke`).
