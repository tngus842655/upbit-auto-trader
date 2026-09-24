/* Upbit Auto Trader 대시보드 — Vue 3 (빌드 없음). 이 파일은 표시·제어 요청만 하고 매매 판단은 하지 않는다. */
(function () {
  const { createApp } = Vue;
  let equityChart = null; // 자산 곡선 Chart.js 인스턴스 (반응형 상태 밖 — Proxy 로 감싸면 Chart.js 내부가 깨진다)
  let btChart = null; // 백테스트 자산 곡선
  const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

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
          { id: "dashboard", label: "대시보드" }, { id: "settings", label: "설정" }, { id: "backtest", label: "백테스트" },
          { id: "control", label: "제어" },
          { id: "pockets", label: "포켓 · 자산 이전" }, { id: "logs", label: "로그" },
        ],
        status: {}, balance: {}, perf: {}, recent: {}, latestSignal: null, logs: [], logLevel: "",
        meta: { available: {}, schemas: {}, intervals: [], risk_schema: {} },
        settingsVersion: 0, settingsHistory: [], form: null, saving: false, saveResult: null, formErrors: [],
        pockets: {}, transfer: { direction: "to_main", amount: 0, bot_pocket_uuid: null }, transferResult: null,
        showDust: false,
        // 코인 선택 팝업 (설정 탭)
        picker: { open: false, loading: false, error: null, data: null, query: "", onlySelected: false, selected: new Set(), target: "settings",
          sort: { key: "acc_trade_price_24h", desc: true } },
        marketCatalog: {},  // 코드 → 한글 이름
        // 백테스트 탭
        bt: { marketsText: "", interval: "", years: {}, recent: { 3: false, 6: false, 12: false }, custom: { enabled: false, start: "", end: "" },
          capital: 1000000, feePct: 0.05, slippagePct: 0.05, useRisk: false, submitting: false, error: null,
          job: null, jobs: [], selectedJobId: "", selected: null, timer: null },
        btDefaults: { years: [], today: "" },
        confirmLive: "", wsConnected: false, ws: null, toast: null, timers: [],
      };
    },
    computed: {
      engine() { return this.status.engine || null; },
      engineAlive() { return !!this.status.engine_alive; },
      engineState() { return this.status.engine_state || "NONE"; },
      engineLabel() {
        const s = this.engineState;
        return { RUNNING: "실행 중", PAUSED: "일시정지", STOPPED: "정지됨", STARTING: "시작 중", STALE: "응답 없음", NONE: "미실행" }[s] || s;
      },
      modeLabel() { return this.mode === "live" ? "실거래" : "모의매매"; },
      selectedMarkets() { return this.form ? this.form.marketsText.split(",").map((m) => m.trim().toUpperCase()).filter(Boolean) : []; },
      currentYear() { return new Date().getFullYear(); },
      btMarkets() { return this.bt.marketsText.split(",").map((m) => m.trim().toUpperCase()).filter(Boolean); },
      btRunning() { return !!this.bt.job && (this.bt.job.status === "queued" || this.bt.job.status === "running"); },
      btProgressPct() { const j = this.bt.job; return j && j.total ? Math.round((j.progress / j.total) * 100) : 0; },
      btPeriods() {
        const out = [];
        for (const y of this.btDefaults.years) if (this.bt.years[y]) out.push({ label: `${y}년`, start: `${y}-01-01`, end: y < this.currentYear ? `${y + 1}-01-01` : null });
        for (const n of [3, 6, 12]) if (this.bt.recent[n]) { const d = new Date(); d.setMonth(d.getMonth() - n); out.push({ label: `최근 ${n}개월`, start: isoDate(d), end: null }); }
        if (this.bt.custom.enabled && this.bt.custom.start) out.push({ label: `${this.bt.custom.start} ~ ${this.bt.custom.end || '오늘'}`, start: this.bt.custom.start, end: this.bt.custom.end || null });
        return out;
      },
      btResult() { const j = this.bt.job; const r = j && this.bt.selected != null ? j.results[this.bt.selected] : null; return r && !r.error ? r : null; },
      btSummary() {
        const j = this.bt.job; if (!j || !j.results.length) return "";
        const ok = j.results.filter((r) => !r.error); if (!ok.length) return "";
        const avg = ok.reduce((a, r) => a + r.metrics.total_return, 0) / ok.length;
        const beat = ok.filter((r) => r.metrics.total_return > r.benchmark.total_return).length;
        const pos = ok.filter((r) => r.metrics.total_return > 0).length;
        return `${ok.length}건 평균 수익률 ${this.pct(avg)} · 수익 난 구간 ${pos}건 · 단순 보유보다 나은 구간 ${beat}건`;
      },
      pickerRows() {
        const data = this.picker.data;
        if (!data) return [];
        const q = this.picker.query.trim().toLowerCase();
        let rows = data.items.filter((r) => !q || r.korean_name.toLowerCase().includes(q) || r.english_name.toLowerCase().includes(q) || r.market.toLowerCase().includes(q) || r.base.toLowerCase().includes(q));
        if (this.picker.onlySelected) rows = rows.filter((r) => this.picker.selected.has(r.market));
        const { key, desc } = this.picker.sort;
        const dir = desc ? -1 : 1;
        return rows.slice().sort((a, b) => {
          const va = a[key], vb = b[key];
          if (typeof va === "string" || typeof vb === "string") return String(va || "").localeCompare(String(vb || ""), "ko") * dir;
          return ((va ?? -Infinity) - (vb ?? -Infinity)) * dir;
        });
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
      // ---------- 한글 표기 (엔진·API 는 영문 코드를 쓰고 화면에서만 바꾼다)
      wsLabel(v) { return { CONNECTED: "연결됨", CONNECTING: "연결 중", RECONNECTING: "재연결 중", DISCONNECTED: "끊김", CLOSED: "종료", NOT_USED: "미사용", ERROR: "오류" }[v] || v || "-"; },
      actionLabel(v) { return { BUY: "매수", SELL: "매도", HOLD: "관망" }[v] || v; },
      sideLabel(v) { return { BUY: "매수", SELL: "매도", bid: "매수", ask: "매도" }[v] || v; },
      orderStatusLabel(v) { return { FILLED: "체결", PARTIAL: "부분 체결", PARTIALLY_FILLED: "부분 체결", REJECTED: "거부", CANCELLED: "취소", CANCELED: "취소", PENDING: "대기", SUBMITTED: "접수", FAILED: "실패" }[v] || v; },
      reasonLabel(v) { return { stop_loss: "손절", take_profit: "익절", trailing_stop: "추적 손절", signal: "신호" }[v] || v; },
      levelLabel(v) { return { INFO: "정보", WARNING: "경고", ERROR: "오류", DEBUG: "디버그" }[v] || v; },
      envModeLabel(v) { return { PAPER: "모의매매", LIVE: "실거래", BACKTEST: "백테스트" }[v] || v || "-"; },
      fieldLabels(list) { const m = { markets: "거래 마켓", strategy_name: "전략", strategy_params: "전략 파라미터", candle_interval: "캔들 단위", risk: "리스크" }; return (list || []).map((f) => m[f] || f).join(", ") || "없음"; },
      channelLabel(v) { return { telegram: "텔레그램", discord: "디스코드", log: "로그" }[v] || v; },
      channelLabels(list) { return (list || []).map((c) => this.channelLabel(c)).join(", ") || "없음"; },
      eventLabel(v) {
        const m = { bot_start: "봇 시작", bot_stop: "봇 종료", command: "명령", stale_commands: "대기 명령 무시", dashboard_start: "대시보드 시작 요청",
          dashboard_kill: "강제 종료", pocket_transfer: "포켓 이전", notify_test: "알림 테스트", notify_failed: "알림 실패", order_filled: "체결",
          order_rejected: "주문 거부", duplicate_order_blocked: "중복 주문 차단", risk_rejected: "리스크 거부", risk_lock: "리스크 잠금",
          paused_skip: "일시정지 중 건너뜀", candle_fetch_failed: "캔들 조회 실패", price_stream_failed: "시세 스트림 끊김", api_error: "API 오류",
          settings_applied: "설정 반영", settings_invalid: "설정 검증 실패", reconcile: "잔고 동기화", signal: "신호", exit: "청산" };
        return m[v] || v;
      },
      commandLabel(v) { return { pause: "일시정지", resume: "재개", stop: "정지", halt: "긴급 정지", resume_risk: "긴급 정지 해제", reload: "설정 다시 읽기", kill: "강제 종료" }[v] || v; },
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
          if (!this.picker.data && !this.picker.loading) this.loadMarketCatalog();  // 선택된 마켓의 한글 이름 표시용
          this.syncBacktestFromSettings();
          if (!this.btDefaults.years.length) this.loadBacktestDefaults();
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
        try { const r = await this.api(`/api/bot/${command}`, { method: "POST", body: payload || {} }); this.notify(`'${this.commandLabel(command)}' 명령 전송 (#${r.id})`, "ok"); }
        catch (e) { this.notify("명령 실패: " + e.message, "bad"); }
      },
      async startEngine() {
        if (this.mode === "live" && !confirm("실제 자금으로 거래를 시작합니다. 계속할까요?")) return;
        try { const r = await this.api("/api/bot/start", { method: "POST", body: { confirm_live: this.confirmLive } }); this.notify(`엔진 시작 (프로세스 ID ${r.pid})`, "ok"); setTimeout(() => this.loadAll(), 3000); }
        catch (e) { this.notify("시작 실패: " + e.message, "bad"); }
      },
      async halt() { const reason = prompt("긴급 정지 사유", "수동 정지"); if (reason != null) await this.cmd("halt", { reason }); },
      async kill() { if (confirm("엔진 프로세스를 강제 종료합니다. 먼저 정지를 시도했나요?")) await this.cmd("kill"); },
      async notifyTest() {
        try {
          const r = await this.api("/api/notify/test", { method: "POST", body: {} });
          const bad = Object.entries(r.results).filter(([, err]) => err);
          if (bad.length) this.notify("알림 일부 실패: " + bad.map(([ch, err]) => this.channelLabel(ch) + " — " + err).join("; "), "bad");
          else this.notify("알림 발송 성공: " + this.channelLabels(Object.keys(r.results)), "ok");
        } catch (e) { this.notify("알림 테스트 실패: " + e.message, "bad"); }
      },
      // ---------- 포켓
      // 0.00000001 같은 먼지 잔고는 기본으로 숨긴다 (KRW 는 항상 표시)
      visibleBalances(list) {
        const rows = list || [];
        return this.showDust ? rows : rows.filter((b) => b.currency === "KRW" || (Number(b.balance) + Number(b.locked)) >= 1e-6);
      },
      // ---------- 코인 선택 팝업
      marketName(code) { return this.marketCatalog[code] || ""; },
      tradeAmount(v) {
        if (v == null) return "-";
        if (v >= 1e12) return (v / 1e12).toFixed(2) + "조 원";
        if (v >= 1e8) return (v / 1e8).toFixed(1) + "억 원";
        return this.krw(v);
      },
      cautionLabel(c) { return { PRICE_FLUCTUATIONS: "가격 급등락", TRADING_VOLUME_SOARING: "거래량 급증", DEPOSIT_AMOUNT_SOARING: "입금 급증", GLOBAL_PRICE_DIFFERENCES: "해외가 차이", CONCENTRATION_OF_SMALL_ACCOUNTS: "소수계정 집중" }[c] || c; },
      async loadMarketCatalog(force) {
        this.picker.loading = true; this.picker.error = null;
        try {
          const data = await this.api("/api/markets?quote=KRW" + (force ? "&refresh=true" : ""));
          this.picker.data = data;
          const names = {};
          for (const r of data.items) names[r.market] = r.korean_name;
          this.marketCatalog = names;
        } catch (e) { this.picker.error = "코인 목록 조회 실패: " + e.message; }
        this.picker.loading = false;
      },
      openMarketPicker(target) {
        this.picker.target = target === "backtest" ? "backtest" : "settings";
        this.picker.selected = new Set(this.picker.target === "backtest" ? this.btMarkets : this.selectedMarkets);
        this.picker.query = ""; this.picker.onlySelected = false; this.picker.open = true;
        if (!this.picker.data && !this.picker.loading) this.loadMarketCatalog();
      },
      togglePick(code) { if (this.picker.selected.has(code)) this.picker.selected.delete(code); else this.picker.selected.add(code); },
      sortBy(key) {
        if (this.picker.sort.key === key) this.picker.sort.desc = !this.picker.sort.desc;
        else this.picker.sort = { key, desc: key !== "korean_name" };
      },
      applyPicker() {
        const text = Array.from(this.picker.selected).join(",");
        if (this.picker.target === "backtest") this.bt.marketsText = text; else this.form.marketsText = text;
        this.picker.open = false;
      },
      removeBtMarket(code) { this.bt.marketsText = this.btMarkets.filter((m) => m !== code).join(","); },
      // ---------- 백테스트
      paramsSummary(params) { return Object.entries(params || {}).map(([k, v]) => `${k}=${v}`).join(", "); },
      btStatusLabel(s) { return { queued: "대기", running: "실행 중", done: "완료", error: "오류", cancelled: "중단" }[s] || s; },
      async loadBacktestDefaults() {
        try {
          const d = await this.api("/api/backtest/defaults");
          this.btDefaults = d;
          const years = {}; for (const y of d.years) years[y] = y >= this.currentYear - 1;  // 올해·작년 기본 선택
          this.bt.years = years;
          this.bt.capital = d.initial_capital; this.bt.feePct = +(d.fee_rate * 100).toFixed(4); this.bt.slippagePct = +(d.slippage_rate * 100).toFixed(4);
          this.bt.jobs = await this.api("/api/backtest/jobs");
        } catch (e) { /* noop */ }
      },
      syncBacktestFromSettings() {
        if (!this.form) return;
        if (!this.bt.marketsText) this.bt.marketsText = this.form.marketsText;
        if (!this.bt.interval) this.bt.interval = this.form.candle_interval;
      },
      async runBacktest() {
        if (!this.form) return;
        const risk = {};
        for (const f of this.riskFields) { const v = this.form.risk[f.name]; risk[f.name] = f.nullable && !this.form.riskEnabled[f.name] ? null : (v === "" ? null : v); }
        const body = { markets: this.btMarkets, candle_interval: this.bt.interval || this.form.candle_interval, strategy_name: this.form.strategy_name,
          strategy_params: this.form.strategy_params, periods: this.btPeriods, initial_capital: this.bt.capital,
          fee_rate: this.bt.feePct / 100, slippage_rate: this.bt.slippagePct / 100, use_risk: this.bt.useRisk, risk: this.bt.useRisk ? risk : null,
          settings_version: this.settingsVersion };
        this.bt.submitting = true; this.bt.error = null; this.bt.selected = null;
        try {
          const job = await this.api("/api/backtest/jobs", { method: "POST", body });
          this.bt.job = { ...job, results: [] }; this.bt.selectedJobId = job.id;
          this.notify(`백테스트 시작 (${job.total}건)`, "ok");
          this.pollBacktest(job.id);
        } catch (e) { this.bt.error = e.message; this.notify("백테스트 실행 실패: " + e.message, "bad"); }
        this.bt.submitting = false;
      },
      pollBacktest(id) {
        if (this.bt.timer) clearTimeout(this.bt.timer);
        this.bt.timer = setTimeout(async () => {
          try {
            const job = await this.api(`/api/backtest/jobs/${id}`);
            this.bt.job = job;
            if (job.status === "queued" || job.status === "running") { this.pollBacktest(id); return; }
            this.bt.jobs = await this.api("/api/backtest/jobs");
            if (this.bt.selected == null) this.selectBtResult(job.results.findIndex((r) => !r.error));
            this.notify(job.status === "done" ? `백테스트 완료: ${job.ok}건 성공, ${job.failed}건 실패` : "백테스트 " + this.btStatusLabel(job.status), job.status === "done" ? "ok" : "bad");
          } catch (e) { this.bt.error = e.message; }
        }, 1500);
      },
      async cancelBacktest() {
        if (!this.bt.job) return;
        try { await this.api(`/api/backtest/jobs/${this.bt.job.id}`, { method: "DELETE" }); } catch (e) { this.notify("중단 실패: " + e.message, "bad"); }
      },
      async loadBacktestJob(id) {
        if (!id) return;
        try {
          this.bt.job = await this.api(`/api/backtest/jobs/${id}`); this.bt.selected = null;
          this.selectBtResult(this.bt.job.results.findIndex((r) => !r.error));
          if (this.btRunning) this.pollBacktest(id);
        } catch (e) { this.notify("결과 조회 실패: " + e.message, "bad"); }
      },
      selectBtResult(i) { if (i == null || i < 0) return; this.bt.selected = i; this.$nextTick(() => this.renderBtChart()); },
      renderBtChart() {
        const el = document.getElementById("btChart"); const r = this.btResult;
        if (!el || !window.Chart || !r) return;
        if (btChart && btChart.canvas !== el) { btChart.destroy(); btChart = null; }
        const data = { labels: r.equity.map((p) => this.fmtTime(p.time)), datasets: [
          { label: "전략", data: r.equity.map((p) => p.value), borderColor: "#4f8cff", backgroundColor: "rgba(79,140,255,0.12)", fill: true, tension: 0.2, pointRadius: 0 },
          { label: "단순 보유", data: r.benchmark_equity.map((p) => p.value), borderColor: "#8a93a6", borderDash: [4, 4], fill: false, tension: 0.2, pointRadius: 0 },
        ] };
        if (btChart) { btChart.data = data; btChart.update(); return; }
        btChart = new Chart(el, { type: "line", data, options: { responsive: true, animation: false, plugins: { legend: { display: true, labels: { color: "#8a93a6" } } },
          scales: { x: { ticks: { maxTicksLimit: 8, color: "#8a93a6" }, grid: { color: "#233" } }, y: { ticks: { color: "#8a93a6", callback: (v) => Math.round(v).toLocaleString() }, grid: { color: "#233" } } } } });
      },
      removeMarket(code) { this.form.marketsText = this.selectedMarkets.filter((m) => m !== code).join(","); },
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
