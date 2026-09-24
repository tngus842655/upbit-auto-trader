/* Upbit Auto Trader 대시보드 — Vue 3 (빌드 없음). 이 파일은 표시·제어 요청만 하고 매매 판단은 하지 않는다. */
(function () {
  const { createApp } = Vue;
  let equityChart = null; // 자산 곡선 Chart.js 인스턴스 (반응형 상태 밖 — Proxy 로 감싸면 Chart.js 내부가 깨진다)

  const RISK_LABELS = {
    position_fraction: "현금 사용 비율", max_order_amount: "거래당 최대 투자금 (KRW)", max_position_ratio: "자산 대비 포지션 상한",
    max_open_positions: "최대 포지션 수", daily_loss_limit_pct: "일일 손실 한도", max_consecutive_losses: "최대 연속 손실",
    stop_loss_pct: "손절 비율", take_profit_pct: "익절 비율", trailing_stop_pct: "추적 손절 비율",
    price_deviation_limit: "시세 괴리 한도", min_order_amount: "최소 주문 금액 (KRW)", cooldown_seconds: "재진입 대기(초)",
  };

  function schemaFields(schema) {
    const out = [];
    const props = (schema && schema.properties) || {};
    for (const [name, prop] of Object.entries(props)) {
      let p = prop, nullable = false;
      if (prop.anyOf) {
        const nonNull = prop.anyOf.filter((x) => x.type !== "null");
        nullable = prop.anyOf.length !== nonNull.length;
        p = Object.assign({}, prop, nonNull[0] || {});
      }
      const type = p.type || "number";
      const min = p.minimum ?? (p.exclusiveMinimum != null ? p.exclusiveMinimum : undefined);
      const max = p.maximum ?? (p.exclusiveMaximum != null ? p.exclusiveMaximum : undefined);
      out.push({ name, type, nullable, min, max, step: type === "integer" ? 1 : "any",
        desc: p.description || prop.description || "", default: prop.default });
    }
    return out;
  }

  createApp({
    data() {
      return {
        mode: localStorage.getItem("mode") || "paper",
        token: localStorage.getItem("token") || "",
        tab: "dashboard",
        tabs: [
          { id: "dashboard", label: "대시보드" }, { id: "settings", label: "설정" }, { id: "control", label: "제어" },
          { id: "pockets", label: "포켓 · 자산 이전" }, { id: "logs", label: "로그" },
        ],
        status: {}, balance: {}, perf: {}, recent: {}, latestSignal: null, logs: [], logLevel: "",
        meta: { available: {}, schemas: {}, intervals: [], risk_schema: {} },
        settingsVersion: 0, settingsHistory: [], form: null, saving: false, saveResult: null, formErrors: [],
        pockets: {}, transfer: { direction: "to_main", amount: 0, bot_pocket_uuid: null }, transferResult: null,
        showDust: false,
        confirmLive: "", wsConnected: false, ws: null, toast: null, timers: [],
      };
    },
    computed: {
      engine() { return this.status.engine || null; },
      engineAlive() { return !!this.status.engine_alive; },
      engineState() { return this.status.engine_state || "NONE"; },
      engineLabel() {
        const s = this.engineState;
        return { RUNNING: "RUNNING", PAUSED: "PAUSED", STOPPED: "STOPPED", STARTING: "STARTING", STALE: "응답 없음", NONE: "미실행" }[s] || s;
      },
      paramFields() { return this.form ? schemaFields(this.meta.schemas[this.form.strategy_name]) : []; },
      riskFields() { return schemaFields(this.meta.risk_schema); },
    },
    methods: {
      // ---------- 유틸
      krw(v) { return v == null || isNaN(v) ? "-" : Math.round(v).toLocaleString("ko-KR") + " 원"; },
      num(v, d) { return v == null || isNaN(v) ? "-" : Number(v).toLocaleString("ko-KR", { maximumFractionDigits: d, minimumFractionDigits: d > 4 ? d : 0 }); },
      pct(v, d) { if (v == null || isNaN(v)) return "-"; const p = v * 100; return (p > 0 ? "+" : "") + p.toFixed(d == null ? 2 : d) + "%"; },
      sign(v) { return v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : ""; },
      fmtTime(iso) { if (!iso) return "-"; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString("ko-KR", { hour12: false }); },
      riskLabel(name) { return RISK_LABELS[name] || name; },
      notify(text, kind) { this.toast = { text, kind: kind || "info" }; setTimeout(() => { if (this.toast && this.toast.text === text) this.toast = null; }, 5000); },
      saveToken() { localStorage.setItem("token", this.token); this.connectWs(); },
      switchMode() { localStorage.setItem("mode", this.mode); this.loadAll(); this.connectWs(); },
      async api(path, opts) {
        const o = Object.assign({ headers: {} }, opts || {});
        if (this.token) o.headers["X-Auth-Token"] = this.token;
        if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
        const sep = path.includes("?") ? "&" : "?";
        const res = await fetch(`${path}${sep}mode=${this.mode}`, o);
        const text = await res.text();
        let data = null; try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
        if (!res.ok) { const d = data && data.detail; throw new Error(Array.isArray(d) ? d.map((x) => `${x.field || ""} ${x.message || x.msg || ""}`).join("; ") : (d || res.statusText)); }
        return data;
      },
      // ---------- 조회
      async loadAll() {
        try {
          const [status, balance, perf, recent] = await Promise.all([
            this.api("/api/status"), this.api("/api/balance"), this.api("/api/performance"), this.api("/api/recent?limit=15"),
          ]);
          this.status = status; this.balance = balance; this.perf = perf; this.recent = recent;
          this.latestSignal = recent.signals && recent.signals.length ? recent.signals[0] : null;
          this.renderChart();
        } catch (e) { this.notify("조회 실패: " + e.message, "bad"); }
        this.loadStrategyMeta();
      },
      async refreshLight() {
        try { const [perf, recent] = await Promise.all([this.api("/api/performance"), this.api("/api/recent?limit=15")]); this.perf = perf; this.recent = recent; this.latestSignal = recent.signals && recent.signals.length ? recent.signals[0] : null; this.renderChart(); } catch (e) { /* 폴링 실패는 조용히 */ }
        if (!this.wsConnected) { try { this.status = await this.api("/api/status"); this.balance = await this.api("/api/balance"); } catch (e) { /* noop */ } }
      },
      async loadStrategyMeta() {
        try { this.meta = await this.api("/api/strategy"); if (!this.form) await this.loadSettings(); } catch (e) { /* noop */ }
      },
      async loadSettings() {
        try {
          const s = await this.api("/api/settings");
          this.settingsVersion = s.version; this.settingsHistory = s.history;
          const d = s.data;
          const riskEnabled = {};
          for (const f of schemaFields(this.meta.risk_schema)) riskEnabled[f.name] = d.risk[f.name] != null;
          this.form = { marketsText: d.markets.join(","), candle_interval: d.candle_interval, strategy_name: d.strategy_name,
            strategy_params: Object.assign({}, d.strategy_params), risk: Object.assign({}, d.risk), riskEnabled, note: "" };
          // 파라미터 기본값 채우기
          for (const f of schemaFields(this.meta.schemas[d.strategy_name])) if (this.form.strategy_params[f.name] == null) this.form.strategy_params[f.name] = f.default;
          this.saveResult = null; this.formErrors = [];
        } catch (e) { this.notify("설정 조회 실패: " + e.message, "bad"); }
      },
      resetParams() {
        const params = {};
        for (const f of schemaFields(this.meta.schemas[this.form.strategy_name])) params[f.name] = f.default;
        this.form.strategy_params = params;
      },
      async saveSettings() {
        if (!this.form) return;
        const risk = {};
        for (const f of this.riskFields) {
          const v = this.form.risk[f.name];
          risk[f.name] = f.nullable && !this.form.riskEnabled[f.name] ? null : (v === "" ? null : v);
        }
        const data = { markets: this.form.marketsText.split(",").map((m) => m.trim()).filter(Boolean),
          strategy_name: this.form.strategy_name, strategy_params: this.form.strategy_params,
          candle_interval: this.form.candle_interval, risk };
        this.saving = true; this.formErrors = [];
        try {
          this.saveResult = await this.api("/api/settings", { method: "PUT", body: { data, note: this.form.note } });
          this.notify(`설정 v${this.saveResult.version} 저장됨`, "ok");
          const s = await this.api("/api/settings"); this.settingsVersion = s.version; this.settingsHistory = s.history;
        } catch (e) { this.formErrors = e.message.split("; "); this.notify("저장 실패", "bad"); }
        this.saving = false;
      },
      // ---------- 제어
      async cmd(command, payload) {
        try { const r = await this.api(`/api/bot/${command}`, { method: "POST", body: payload || {} }); this.notify(`명령 '${command}' 전송 (#${r.id})`, "ok"); }
        catch (e) { this.notify("명령 실패: " + e.message, "bad"); }
      },
      async startEngine() {
        if (this.mode === "live" && !confirm("실제 자금으로 거래를 시작합니다. 계속할까요?")) return;
        try { const r = await this.api("/api/bot/start", { method: "POST", body: { confirm_live: this.confirmLive } }); this.notify(`엔진 시작 (pid ${r.pid})`, "ok"); setTimeout(() => this.loadAll(), 3000); }
        catch (e) { this.notify("시작 실패: " + e.message, "bad"); }
      },
      async halt() { const reason = prompt("긴급 정지 사유", "수동 정지"); if (reason != null) await this.cmd("halt", { reason }); },
      async kill() { if (confirm("엔진 프로세스를 강제 종료합니다. 먼저 Stop 을 시도했나요?")) await this.cmd("kill"); },
      async notifyTest() {
        try {
          const r = await this.api("/api/notify/test", { method: "POST", body: {} });
          const bad = Object.entries(r.results).filter(([, err]) => err);
          if (bad.length) this.notify("알림 일부 실패: " + bad.map(([ch, err]) => ch + " — " + err).join("; "), "bad");
          else this.notify("알림 발송 성공: " + Object.keys(r.results).join(", "), "ok");
        } catch (e) { this.notify("알림 테스트 실패: " + e.message, "bad"); }
      },
      // ---------- 포켓
      // 0.00000001 같은 먼지 잔고는 기본으로 숨긴다 (KRW 는 항상 표시)
      visibleBalances(list) {
        const rows = list || [];
        return this.showDust ? rows : rows.filter((b) => b.currency === "KRW" || (Number(b.balance) + Number(b.locked)) >= 1e-6);
      },
      async loadPockets() { try { this.pockets = await this.api("/api/pockets"); } catch (e) { this.pockets = { error: e.message }; } },
      async doTransfer() {
        if (!confirm(`${this.transfer.direction === "to_bot" ? "메인 → 봇 포켓" : "봇 포켓 → 메인"} 으로 ${this.krw(this.transfer.amount)} 을 이전합니다. 계속할까요?`)) return;
        try { this.transferResult = await this.api("/api/pockets/transfer", { method: "POST", body: { direction: this.transfer.direction, amount: this.transfer.amount, currency: "KRW", bot_pocket_uuid: this.transfer.bot_pocket_uuid } }); this.notify("이전 요청 접수", "ok"); setTimeout(() => this.loadPockets(), 2000); }
        catch (e) { this.notify("이전 실패: " + e.message, "bad"); }
      },
      async loadLogs() { try { this.logs = await this.api(`/api/logs?limit=200${this.logLevel ? "&level=" + this.logLevel : ""}`); } catch (e) { this.notify("로그 조회 실패: " + e.message, "bad"); } },
      // ---------- 실시간
      connectWs() {
        if (this.ws) { try { this.ws.close(); } catch (e) { /* noop */ } this.ws = null; }
        const proto = location.protocol === "https:" ? "wss" : "ws";
        const url = `${proto}://${location.host}/ws?mode=${this.mode}${this.token ? "&token=" + encodeURIComponent(this.token) : ""}`;
        const ws = new WebSocket(url);
        ws.onopen = () => { this.wsConnected = true; };
        ws.onclose = () => { this.wsConnected = false; setTimeout(() => { if (this.ws === ws) this.connectWs(); }, 5000); };
        ws.onerror = () => { this.wsConnected = false; };
        ws.onmessage = (ev) => {
          try { const m = JSON.parse(ev.data); if (m.type === "tick") { this.status = m.status; this.balance = m.balance; if (m.latest_signal) this.latestSignal = m.latest_signal; } } catch (e) { /* noop */ }
        };
        this.ws = ws;
      },
      renderChart() {
        const el = document.getElementById("equityChart");
        if (!el || !window.Chart) return;
        const series = (this.perf && this.perf.series) || [];
        const labels = series.map((p) => this.fmtTime(p.time));
        const data = series.map((p) => p.equity);
        // Chart 인스턴스는 Vue 반응형 상태 밖(모듈 변수)에 둔다 — Proxy 로 감싸면 Chart.js 내부가 깨진다
        if (!equityChart) {
          equityChart = new Chart(el, { type: "line", data: { labels, datasets: [{ label: "자산 (KRW)", data, borderColor: "#4f8cff", backgroundColor: "rgba(79,140,255,0.15)", fill: true, tension: 0.2, pointRadius: 0 }] },
            options: { responsive: true, animation: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { maxTicksLimit: 6, color: "#8a93a6" }, grid: { color: "#233" } }, y: { ticks: { color: "#8a93a6", callback: (v) => Math.round(v).toLocaleString() }, grid: { color: "#233" } } } } });
        } else { equityChart.data.labels = labels; equityChart.data.datasets[0].data = data; equityChart.update(); }
      },
    },
    mounted() {
      this.loadAll(); this.connectWs(); this.loadLogs(); this.loadPockets();
      this.timers.push(setInterval(() => this.refreshLight(), 15000));
      this.timers.push(setInterval(() => { if (this.tab === "logs") this.loadLogs(); }, 15000));
    },
  }).mount("#app");
})();
