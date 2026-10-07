// Logo tim, dipakai di command center dan halaman masuk.
//
// Setiap elemen bertanda data-logo (tanda "SOS" merah) diganti dengan gambar logo.
// Urutan sumber: dashboard/vendor/images/logo.png (tempat gambar project ini), lalu dashboard/logo.png,
// lalu tautan logo tim.
// Kalau dua-duanya gagal dimuat, tanda "SOS" merah tetap tampil, jadi header tidak pernah kosong.
// Logo yang sama juga dipasang sebagai ikon tab browser.
//
// Logonya berwarna gelap di atas dasar terang, sedangkan halaman ini gelap,
// karena itu logo ditaruh di atas ubin putih.
(function () {
  "use strict";
  var sources = ["vendor/images/logo.png", "logo.png", "https://i.postimg.cc/m2mKK4yP/Chat-GPT-Image-Sep-30-2026-12-17-32-AM.png"];

  var style = document.createElement("style");
  style.textContent =
    ".logo-tile { display: inline-flex; align-items: center; justify-content: center; flex: none; padding: 3px 5px; border-radius: 10px; background: #fff; box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.14), 0 4px 14px rgba(0, 0, 0, 0.35); }" +
    ".logo-tile img { display: block; height: 34px; width: auto; max-width: 120px; object-fit: contain; }" +
    ".logo-tile.sm { padding: 2px 4px; border-radius: 7px; }" +
    ".logo-tile.sm img { height: 24px; max-width: 84px; }";
  document.head.appendChild(style);

  var found = null;   // sumber logo yang berhasil dimuat

  function apply(src) {
    var marks = document.querySelectorAll("[data-logo]");
    for (var i = 0; i < marks.length; i++) {
      var tile = document.createElement("span");
      tile.className = "logo-tile" + (marks[i].getAttribute("data-logo") === "sm" ? " sm" : "");
      var img = document.createElement("img");
      img.src = src;
      img.alt = "";
      tile.appendChild(img);
      marks[i].replaceWith(tile);
    }
  }
  // Ikon kecil di tab browser. Logonya dipasang langsung dulu; kalau browser mengizinkan gambarnya
  // dibaca, logo digambar ulang di atas kotak putih 64 x 64 supaya tidak gepeng dan tetap jelas di tab gelap.
  function tabIcon(src) {
    var link = document.querySelector('link[rel~="icon"]');
    if (!link) { link = document.createElement("link"); link.rel = "icon"; document.head.appendChild(link); }
    link.type = "image/png";
    link.href = src;
    var img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = function () {
      try {
        var size = 64, pad = 5, c = document.createElement("canvas");
        c.width = c.height = size;
        var g = c.getContext("2d");
        g.fillStyle = "#fff";
        g.beginPath();
        if (g.roundRect) g.roundRect(0, 0, size, size, 12); else g.rect(0, 0, size, size);
        g.fill();
        var k = Math.min((size - pad * 2) / img.naturalWidth, (size - pad * 2) / img.naturalHeight);
        var w = img.naturalWidth * k, h = img.naturalHeight * k;
        g.drawImage(img, (size - w) / 2, (size - h) / 2, w, h);
        link.href = c.toDataURL("image/png");
      } catch (e) { /* gambar dari situs lain tanpa izin baca: ikon langsung dari tautannya tetap dipakai */ }
    };
    img.src = src;
  }

  function tryNext() {
    var src = sources.shift();
    if (!src) return;
    var probe = new Image();
    probe.onload = function () { if (probe.naturalWidth > 1) { found = src; apply(src); tabIcon(src); } else tryNext(); };
    probe.onerror = tryNext;
    probe.src = src;
  }
  tryNext();

  // Sebagian tampilan digambar belakangan oleh JavaScript (misalnya menu samping command center),
  // dan digambar ulang setiap isinya berubah. Tanda "SOS" yang baru muncul ikut diganti di sini,
  // sebelum sempat terlihat di layar.
  if (window.MutationObserver) {
    new MutationObserver(function () {
      if (found && document.querySelector("[data-logo]")) apply(found);
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
})();
