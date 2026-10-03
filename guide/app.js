(function () {
  "use strict";

  var DATA = window.GUIDE_DATA;
  var C = window.GUIDE_CONTENT;
  var FRAME_HEADER_BYTES = 7;
  var CRC_BYTES = 4;
  var TAG_COLOR = { tx: "--tx-line", rx: "--rx-line", ch: "--ch-line", ext: "--ext-line", ctl: "--ctl-line", ins: "--ins-line" };

  var state = { transport: "sim", module: "tx-phy", block: 0, step: -1, timer: null, frag: 0 };

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (html !== undefined) { node.innerHTML = html; }
    return node;
  }
  // A content value is either fixed or keyed by sample transport.
  function pick(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value[state.transport] : value;
  }
  function list(items) { return items.map(function (i) { return "<div>" + i + "</div>"; }).join(""); }
  function moduleById(id) { return C.MODULES.filter(function (m) { return m.id === id; })[0]; }
  function fmt(n) { return n.toLocaleString("en-US"); }

  /* ---------------- tabs ---------------- */
  var tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
  function selectTab(tab) {
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      $(t.getAttribute("aria-controls")).hidden = !on;
    });
    if (tab.id !== "tab-flow") { stop(); }
    if (tab.id === "tab-lab") { drawConstellation(); }
  }
  tabs.forEach(function (tab, i) {
    tab.addEventListener("click", function () { selectTab(tab); });
    tab.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") { return; }
      var next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      next.focus();
      selectTab(next);
    });
  });

  /* ---------------- transport ---------------- */
  document.querySelectorAll("[data-transport]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.transport = btn.getAttribute("data-transport");
      document.querySelectorAll("[data-transport]").forEach(function (other) {
        other.setAttribute("aria-pressed", String(other === btn));
      });
      renderFlow();
      renderDetail();
      renderRun();
      renderCode();
    });
  });

  /* ---------------- workflow ---------------- */
  // Block ids in the SVG diagram -> [module id, block index] in content.js.
  var SVG_BLOCKS = {
    "ext-in": ["ext-in", 0], "tx-in": ["tx-app", 0], "tx-dgram": ["tx-link", 0], "tx-frame": ["tx-link", 1],
    "tx-fec": ["tx-phy", 0], "tx-map": ["tx-phy", 1], "tx-ofdm": ["tx-phy", 2], "tx-burst": ["tx-phy", 3],
    "tx-peak": ["tx-tr", 0], "tx-sink": ["tx-tr", 1], "channel": ["channel", 0], "rx-src": ["rx-tr", 0],
    "rx-acq": ["rx-phy", 0], "rx-header": ["rx-phy", 1], "rx-eq": ["rx-phy", 2], "rx-demap": ["rx-phy", 3], "rx-fec": ["rx-phy", 4],
    "rx-frame": ["rx-link", 0], "rx-dgram": ["rx-link", 1], "rx-out": ["rx-app", 0], "ext-out": ["ext-out", 0],
    "mcs": ["mcs", 0], "gui-spectrum": ["spec", 0], "gui-const": ["const", 0], "gui-stats": ["stats", 0]
  };
  var chain = $("chain");
  var svgBlocks = Array.prototype.slice.call(chain.querySelectorAll(".blk"));
  function renderFlow() {
    var active = state.step >= 0 ? C.TOUR[state.step].mod : null;
    chain.setAttribute("data-mode", state.transport);
    chain.classList.toggle("touring", active !== null);
    svgBlocks.forEach(function (b) {
      var ref = SVG_BLOCKS[b.getAttribute("data-id")];
      var on = ref[0] === state.module && ref[1] === state.block;
      b.classList.toggle("selected", on);
      b.classList.toggle("active", ref[0] === active);
      b.setAttribute("aria-pressed", String(on));
    });
  }
  function selectBlock(ref) {
    stop();
    if (state.step >= 0 && C.TOUR[state.step].mod !== ref[0]) { state.step = -1; renderShape(); }
    state.module = ref[0];
    state.block = ref[1];
    renderFlow();
    renderDetail();
  }
  svgBlocks.forEach(function (b) {
    var ref = SVG_BLOCKS[b.getAttribute("data-id")];
    b.setAttribute("aria-label", moduleById(ref[0]).blocks[ref[1]].name);
    b.addEventListener("click", function () { selectBlock(ref); });
    b.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectBlock(ref); }
    });
  });

  function renderDetail() {
    var m = moduleById(state.module);
    var blk = m.blocks[state.block];
    var color = "var(" + TAG_COLOR[m.cls] + ")";
    var html = '<h2><span class="tag" style="color:' + color + ";border-color:" + color + '">' + (m.row || "Channel") + "</span>" + m.name + "</h2>" +
      '<p class="sum">' + m.sum + "</p>";
    if (m.blocks.length > 1) {
      html += '<div class="chain" role="group" aria-label="這個模組的 blocks">' + m.blocks.map(function (b, i) {
        return (i ? '<span class="sep">→</span>' : "") + '<button type="button" data-block="' + i + '" aria-pressed="' + (i === state.block) + '">' + b.name + "</button>";
      }).join("") + "</div>";
    }
    html += '<div class="facts"><div class="does">' +
      (m.blocks.length > 1 ? "<h2>" + blk.name + "</h2>" : "") +
      pick(blk.does).map(function (s) { return "<p>" + s + "</p>"; }).join("") +
      '<div class="io"><span>' + pick(blk.input) + "</span>→<span>" + pick(blk.output) + "</span></div></div>" +
      "<dl><dt>程式位置</dt><dd>" + list(pick(blk.code)) + "</dd>" +
      "<dt>相關選項</dt><dd>" + list(pick(blk.opts)) + "</dd>" +
      "<dt>在 GUI 的位置</dt><dd>" + blk.gui + "</dd></dl></div>";
    $("detail").innerHTML = html;
    $("detail").querySelectorAll("[data-block]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.block = Number(btn.getAttribute("data-block"));
        renderFlow();
        renderDetail();
      });
    });
  }

  function renderShape() {
    var box = $("shape");
    if (state.step < 0) {
      $("stepno").textContent = "";
      box.innerHTML = '<p class="cap">請按<b>「跟著一則 message 走」</b>，查看資料在每個模組的變化。</p>';
      return;
    }
    var s = C.TOUR[state.step];
    $("stepno").textContent = "第 " + (state.step + 1) + " 步，共 " + C.TOUR.length + " 步";
    box.innerHTML = '<p class="cap"><b>' + (state.step + 1) + ".</b> " + s.cap + "</p>" +
      '<div class="segbar">' + s.shape.map(function (seg, i) {
        var grow = seg[0] === "s-p" ? 4 : 1;
        return '<div class="' + seg[0] + '" style="flex:' + grow + ' 1 0">' + seg[1] + "</div>";
      }).join("") + "</div>";
  }
  function goTo(step) {
    state.step = (step + C.TOUR.length) % C.TOUR.length;
    state.module = C.TOUR[state.step].mod;
    state.block = 0;
    renderFlow();
    renderDetail();
    renderShape();
  }
  function stop() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    $("play").textContent = "▶ 跟著一則 message 走";
  }
  $("play").addEventListener("click", function () {
    if (state.timer) { stop(); return; }
    goTo(state.step < 0 || state.step === C.TOUR.length - 1 ? 0 : state.step + 1);
    $("play").textContent = "❚❚ 暫停";
    state.timer = setInterval(function () {
      if (state.step === C.TOUR.length - 1) { stop(); return; }
      goTo(state.step + 1);
    }, 2200);
  });
  $("next").addEventListener("click", function () { stop(); goTo(state.step + 1); });
  $("prev").addEventListener("click", function () { stop(); goTo(state.step < 0 ? 0 : state.step - 1); });

  /* ---------------- packet lab ---------------- */
  var mcsSelect = $("mcs");
  DATA.mcs.forEach(function (m) {
    var o = el("option", null, m.label);
    o.value = m.index;
    mcsSelect.appendChild(o);
  });

  // Payload OFDM symbols for one fragment, from the run-length table in data.js.
  function payloadSymbols(mcs, bytes) {
    var count = mcs.steps[0][1];
    for (var i = 0; i < mcs.steps.length && mcs.steps[i][0] <= bytes; i++) { count = mcs.steps[i][1]; }
    return count;
  }

  function seg(cls, flex, title, sub) {
    return '<div class="' + cls + '" style="flex:' + flex + '"><span><b>' + title + "</b>" + (sub ? "<br>" + sub : "") + "</span></div>";
  }

  function renderLab() {
    var size = Number($("size").value);
    var mcs = DATA.mcs[Number(mcsSelect.value)];
    var rate = Number($("rate").value);
    var symLen = DATA.fftLen + DATA.cpLen;
    var max = DATA.maxFragmentBytes;

    var fragments = [];
    for (var left = size; left > 0; left -= max) { fragments.push(Math.min(left, max)); }
    if (state.frag >= fragments.length) { state.frag = 0; }
    var symbols = fragments.map(function (bytes) { return payloadSymbols(mcs, bytes); });
    var samples = symbols.map(function (n) { return (DATA.fixedSymbols + n) * symLen + 2 * DATA.guardSamples; });
    var total = samples.reduce(function (a, b) { return a + b; }, 0);
    var airtimeMs = total / rate * 1e3;

    $("size-out").textContent = fmt(size) + " bytes";
    $("tiles").innerHTML = [
      ["Bursts", fmt(fragments.length), ""],
      ["Samples", fmt(total), ""],
      ["Airtime", airtimeMs.toFixed(2), "ms"],
      ["發射期間的 rate", (size * 8 / (total / rate) / 1e6).toFixed(2), "Mbit/s"]
    ].map(function (t) {
      return '<div class="tile"><div class="k">' + t[0] + '</div><div class="v">' + t[1] + '<span class="u">' + t[2] + "</span></div></div>";
    }).join("");

    $("frags").innerHTML = fragments.map(function (bytes, i) {
      return '<button type="button" data-frag="' + i + '" aria-pressed="' + (i === state.frag) + '">#' + (i + 1) + " · " + bytes + " B</button>";
    }).join("");
    $("frags").querySelectorAll("[data-frag]").forEach(function (btn) {
      btn.addEventListener("click", function () { state.frag = Number(btn.getAttribute("data-frag")); renderLab(); });
    });
    $("frag-hint").textContent = fragments.length === 1
      ? "這則 message 放進一個 fragment。"
      : "這 " + fragments.length + " 個 bursts 遺失任何一個時，RX 丟棄整則 message。";

    var bytes = fragments[state.frag];
    var dgram = bytes + DATA.datagramHeaderBytes;
    var frame = dgram + FRAME_HEADER_BYTES + CRC_BYTES;
    var n = symbols[state.frag];
    function layer(name, sub, bar) {
      return '<div class="layer"><div class="name">' + name + "<small>" + sub + '</small></div><div class="segbar">' + bar + "</div></div>";
    }
    $("stack").innerHTML =
      layer("Fragment " + (state.frag + 1), bytes + " B", seg("s-p", "1", "application bytes")) +
      '<div class="down">↓ datagram.py 加上 reassembly 用的 header</div>' +
      layer("Datagram", dgram + " B", seg("s-h", "0 0 30%", "header", "28 B") + seg("s-p", "1", "fragment", bytes + " B")) +
      '<div class="down">↓ PHY frame codec 加上 sequence number 與 CRC</div>' +
      layer("PHY frame", frame + " B = " + fmt(frame * 8) + " bits",
        seg("s-h", "0 0 16%", "header", "7 B") + seg("s-p", "1", "datagram", dgram + " B") + seg("s-c", "0 0 16%", "CRC-32", "4 B")) +
      '<div class="down">↓ ' + mcs.label.replace(/^MCS \d+ — /, "") + "</div>" +
      layer("Payload", n + " OFDM symbols", seg("s-p", "1", n + " × 48 data subcarriers", "最後一個 OFDM symbol 可能含有 padding"));

    var parts = [
      ["b-g", "Guard（zeros）", DATA.guardSamples],
      ["b-pre", "Preamble：acquisition", symLen],
      ["b-tr", "Training：channel estimation", symLen],
      ["b-hd", "Header × 3：MCS 與 length", 3 * symLen],
      ["b-pl", "Payload × " + n, n * symLen],
      ["b-g", "Guard（zeros）", DATA.guardSamples]
    ];
    $("burst").innerHTML = parts.map(function (p) {
      return '<div class="' + p[0] + '" style="flex:' + p[2] + ' 1 0" title="' + p[1] + ": " + p[2] + ' samples"></div>';
    }).join("");
    $("burst-legend").innerHTML = parts.slice(0, 5).map(function (p) {
      return '<li><i class="' + p[0] + '"></i>' + p[1] + '<span class="cnt">' + fmt(p[2]) + (p[0] === "b-g" ? " × 2" : "") + "</span></li>";
    }).join("");
    $("burst-total").innerHTML = "<b>Burst " + (state.frag + 1) + "：</b>" + fmt(samples[state.frag]) + " samples = " +
      (samples[state.frag] / rate * 1e3).toFixed(2) + " ms（" + rate / 1e6 + " MS/s，包含 guard）";

    $("mcs-table").innerHTML = '<thead><tr><th>MCS</th><th class="num">Payload symbols</th><th class="num">Airtime</th></tr></thead><tbody>' +
      DATA.mcs.map(function (m) {
        var count = payloadSymbols(m, bytes);
        return '<tr data-mcs="' + m.index + '"' + (m === mcs ? ' class="on"' : "") + "><td>" + m.label + '</td><td class="num">' + count +
          '</td><td class="num">' + ((DATA.fixedSymbols + count) * symLen / rate * 1e3).toFixed(2) + " ms</td></tr>";
      }).join("") + "</tbody>";
    $("mcs-table").querySelectorAll("[data-mcs]").forEach(function (row) {
      row.addEventListener("click", function () { mcsSelect.value = row.getAttribute("data-mcs"); renderLab(); });
    });

    renderLight();
    drawConstellation();
  }

  // Same rule order as dashboard.rx_light, for the case with zero loss.
  function renderLight() {
    var mcs = DATA.mcs[Number(mcsSelect.value)];
    var snr = Number($("snr").value);
    var thr = mcs.snrThresholdDb;
    $("snr-out").textContent = snr.toFixed(1) + " dB";
    var name = "GOOD", color = "--good", reason;
    if (thr === null) {
      reason = "沒有 loss。MCS " + mcs.index + " 沒有量測過的 SNR threshold。";
    } else if (snr < thr + DATA.marginDb) {
      name = "MARGINAL"; color = "--warn";
      reason = "SNR 小於 " + (thr + DATA.marginDb) + " dB。MCS " + mcs.index + " 需要 " + thr + " dB。";
      var better = DATA.mcs.filter(function (m) { return m.snrThresholdDb !== null && m.snrThresholdDb < thr; })
        .sort(function (a, b) { return a.snrThresholdDb - b.snrThresholdDb; });
      if (snr < thr && better.length) {
        var enough = better.filter(function (m) { return m.snrThresholdDb <= snr; });
        reason += "請試 MCS " + (enough.length ? enough[enough.length - 1] : better[0]).index + "。";
      }
    } else {
      reason = "沒有 loss。SNR 比 MCS " + mcs.index + " 的 threshold 高 " + (snr - thr).toFixed(1) + " dB。";
    }
    $("light").innerHTML = '<i class="dot" style="background:var(' + color + ')"></i><div><b>' + name + "</b><span>" + reason + "</span></div>";
    $("light-note").textContent = thr !== null && snr < thr
      ? "沒有 burst 遺失時，light 才顯示這個狀態。SNR 低於 threshold 時，通常會有 loss。這時 light 顯示 LOSS 或 LOSSY。"
      : "最近 10 秒沒有 burst 遺失時，light 才顯示這個狀態。";
  }

  function drawConstellation() {
    var canvas = $("const");
    if (!canvas.offsetParent) { return; }
    var ctx = canvas.getContext("2d");
    var css = getComputedStyle(document.documentElement);
    var mcs = DATA.mcs[Number(mcsSelect.value)];
    var qam = /16QAM/.test(mcs.label);
    var levels = qam ? [-3, -1, 1, 3] : [-1, 1];
    var norm = Math.sqrt(qam ? 10 : 2);  // unit average symbol energy
    var sigma = Math.sqrt(Math.pow(10, -Number($("snr").value) / 10) / 2);
    var w = canvas.width, scale = w / 3.4;
    function px(v) { return w / 2 + v * scale; }
    var seed = 12345;  // fixed seed: the cloud changes with the SNR only
    function rnd() { seed = (seed * 1664525 + 1013904223) >>> 0; return (seed + 0.5) / 4294967296; }
    function gauss() { return Math.sqrt(-2 * Math.log(rnd())) * Math.cos(2 * Math.PI * rnd()); }

    ctx.clearRect(0, 0, w, w);
    ctx.strokeStyle = css.getPropertyValue("--line");
    ctx.beginPath(); ctx.moveTo(w / 2, 0); ctx.lineTo(w / 2, w); ctx.moveTo(0, w / 2); ctx.lineTo(w, w / 2); ctx.stroke();
    ctx.fillStyle = css.getPropertyValue("--rx-line");
    ctx.globalAlpha = 0.55;
    for (var i = 0; i < 640; i++) {
      var re = levels[Math.floor(rnd() * levels.length)] / norm + sigma * gauss();
      var im = levels[Math.floor(rnd() * levels.length)] / norm + sigma * gauss();
      ctx.beginPath(); ctx.arc(px(re), px(-im), 2, 0, 2 * Math.PI); ctx.fill();
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = css.getPropertyValue("--sel");
    levels.forEach(function (a) {
      levels.forEach(function (b) {
        ctx.beginPath(); ctx.arc(px(a / norm), px(b / norm), 4.5, 0, 2 * Math.PI); ctx.fill();
      });
    });
  }

  ["size", "mcs", "rate"].forEach(function (id) { $(id).addEventListener("input", renderLab); });
  $("snr").addEventListener("input", function () { renderLight(); drawConstellation(); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawConstellation);

  // Subcarrier map: bins -32..31, used -26..-1 and 1..26, pilots at +-7 and +-21.
  (function () {
    var pilots = [-21, -7, 7, 21];
    for (var k = -32; k <= 31; k++) {
      var kind = k === 0 ? "dc" : Math.abs(k) > 26 ? "n" : pilots.indexOf(k) >= 0 ? "pl" : "d";
      var cell = el("div", kind);
      cell.title = "Subcarrier " + k + ": " + { dc: "DC（null）", n: "guard band（null）", pl: "pilot", d: "data" }[kind];
      $("sc").appendChild(cell);
      $("sc-axis").appendChild(el("span", null, [-32, -21, -7, 0, 7, 21, 31].indexOf(k) >= 0 ? String(k) : ""));
    }
  })();

  /* ---------------- code structure ---------------- */
  function renderCode() {
    var names = { sim: ["UdpSampleSink", "UdpSampleSource"], uhd: ["UhdSampleSink", "UhdSampleSource"], pluto: ["PlutoSampleSink", "PlutoSampleSource"] }[state.transport];
    document.querySelector("[data-sink]").innerHTML = "<code>" + names[0] + "</code>：把 samples 送到 channel";
    document.querySelector("[data-source]").innerHTML = "<code>" + names[1] + "</code>：提供連續的 sample stream";
  }
  $("files").innerHTML = "<thead><tr><th>檔案</th><th>對應的 block</th><th>功能</th></tr></thead><tbody>" +
    C.FILES.map(function (f) {
      return "<tr><td>" + f[0].split("、").map(function (n) { return "<code>" + n + "</code>"; }).join("、") + "</td><td>" + f[1] + "</td><td>" + f[2] + "</td></tr>";
    }).join("") + "</tbody>";

  /* ---------------- run it ---------------- */
  function renderRun() {
    var run = C.RUN[state.transport];
    $("run-title").textContent = run.title;
    var steps = $("steps");
    steps.innerHTML = "";
    run.steps.forEach(function (s) {
      var li = el("li");
      if (s.caution) { li.appendChild(el("p", "caution", "<b>注意：</b>" + s.caution)); }
      if (s.text) { li.appendChild(el("p", null, s.text)); }
      if (s.cmd) {
        var box = el("div", "cmd");
        var pre = el("pre");
        pre.textContent = s.cmd;
        var copy = el("button", "btn", "複製");
        copy.type = "button";
        copy.addEventListener("click", function () {
          if (!navigator.clipboard) { return; }
          navigator.clipboard.writeText(s.cmd).then(function () {
            copy.textContent = "已複製";
            setTimeout(function () { copy.textContent = "複製"; }, 1500);
          });
        });
        box.appendChild(pre);
        box.appendChild(copy);
        li.appendChild(box);
      }
      steps.appendChild(li);
    });
  }
  C.SYMPTOMS.forEach(function (s) {
    var b = el("button", null, s.q);
    b.type = "button";
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", function () {
      $("symptoms").querySelectorAll("button").forEach(function (o) { o.setAttribute("aria-pressed", String(o === b)); });
      $("fix").innerHTML = "<dl><dt>原因</dt><dd>" + s.cause + "</dd><dt>修正方式</dt><dd>" + s.fix + "</dd></dl>";
    });
    $("symptoms").appendChild(b);
  });

  renderFlow();
  renderDetail();
  renderShape();
  renderLab();
  renderRun();
  renderCode();

  // index.html#lab and index.html#run open that view directly.
  var linked = $("tab-" + location.hash.slice(1));
  if (linked) { selectTab(linked); }
})();
