/* Upbit Auto Trader 대시보드 — Vue 3 (빌드 없음). 이 파일은 표시·제어 요청만 하고 매매 판단은 하지 않는다. */
(function () {
  const { createApp } = Vue;
  let equityChart = null; // 자산 곡선 Chart.js 인스턴스 (반응형 상태 밖 — Proxy 로 감싸면 Chart.js 내부가 깨진다)
  let btChart = null; // 백테스트 자산 곡선
  const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

  const RISK_LABELS = {
    position_fraction: "현금 사용 비율 (%)", max_order_amount: "거래당 최대 투자금 (KRW)", max_position_ratio: "자산 대비 포지션 상한 (%)",
    max_open_positions: "최대 포지션 수", daily_loss_limit_pct: "일일 손실 한도 (%)", max_consecutive_losses: "최대 연속 손실",
    stop_loss_pct: "손절 비율 (%)", take_profit_pct: "익절 비율 (%)", trailing_stop_pct: "추적 손절 비율 (%)",
    price_deviation_limit: "시세 괴리 한도 (%)", min_order_amount: "최소 주문 금액 (KRW)", cooldown_seconds: "재진입 대기(초)",
  };
  // 엔진·API 는 0~1 소수(0.05 = 5%)를 쓰고, 화면에서는 0~100 % 로 보여주고 입력받는다
  const PERCENT_FIELDS = new Set(["position_fraction", "max_position_ratio", "daily_loss_limit_pct", "stop_loss_pct",
    "take_profit_pct", "trailing_stop_pct", "price_deviation_limit"]);
  const toPercent = (v) => (v == null || v === "" ? v : Math.round(Number(v) * 100 * 10000) / 10000);
  const fromPercent = (v) => (v == null || v === "" ? v : Math.round(Number(v) / 100 * 1e8) / 1e8);
  // 항목별 도움말 (? 아이콘 툴팁)
  const HELP = {
    markets: "봇이 거래할 원화 마켓 목록입니다. 여기 없는 코인은 보지도, 팔지도 않습니다.\n바꾸면 엔진 재시작(정지 → 시작)이 필요합니다.",
    candle_interval: "전략이 판단하는 캔들 길이입니다. 캔들이 닫힐 때마다 한 번 판단하므로 짧을수록 신호와 거래가 잦고 수수료 부담이 커집니다.\n주·월·연 캔들은 엔진에서 쓸 수 없습니다. 바꾸면 엔진 재시작이 필요합니다.",
    strategy_name: "신호를 만드는 규칙입니다.\nma_cross = 단기/장기 이동평균 교차(추세 추종)\nrsi = RSI 과매도 탈출 매수 / 과매수 이탈 매도\n바꾸면 파라미터가 기본값으로 초기화됩니다.",
    short_window: "단기 이동평균 기간(캔들 수)입니다. 단기선이 장기선을 아래→위로 뚫는 캔들(골든크로스)에서 매수 신호, 위→아래(데드크로스)에서 매도 신호가 납니다.\n값을 줄이면 신호가 잦아지지만 잔파도에 자주 걸리고, 키우면 느리지만 큰 추세만 탑니다.",
    long_window: "장기 이동평균 기간(캔들 수)입니다. 단기선보다 커야 합니다.\n이 길이만큼 캔들이 쌓여야 첫 신호가 납니다(15분봉 200 = 약 50시간).",
    volume_window: "거래량 필터의 평균 기간(캔들 수)입니다. 0이면 거래량 필터를 끕니다.",
    volume_factor: "골든크로스 캔들의 거래량이 '최근 N캔들 평균 × 이 배수' 이상일 때만 매수합니다. 1 = 평균 이상, 2 = 평균의 2배 이상.\n거래량 없는 밋밋한 교차를 거릅니다. 매도에는 적용되지 않고, 걸러진 신호는 다음 골든크로스까지 다시 시도하지 않습니다.",
    rsi_window: "RSI 계산 기간(캔들 수)입니다. 0이면 RSI 필터를 끄고 오른쪽 값은 무시됩니다.",
    rsi_max_for_buy: "골든크로스라도 RSI가 이 값보다 높으면(과매수) 매수하지 않습니다. 매도에는 영향이 없습니다. 보통 70~80.",
    window: "RSI 계산 기간(캔들 수)입니다. 짧을수록 민감하게 움직입니다. 보통 14.",
    oversold: "과매도선입니다. RSI가 이 값 아래로 내려갔다가 다시 위로 올라오는 캔들에서 매수합니다(과매도 탈출). 보통 25~35.",
    overbought: "과매수선입니다. RSI가 이 값 위에 있다가 아래로 내려오는 캔들에서 매도합니다(과매수 이탈). 보통 65~75. 과매도선보다 커야 합니다.",
    position_fraction: "한 번 매수할 때 현재 현금의 몇 퍼센트까지 쓸지 정합니다. 100% = 현금 전부, 50% = 절반.\n'거래당 최대 투자금'·'자산 대비 포지션 상한'이 이 예산을 더 줄일 수 있습니다.",
    max_order_amount: "한 번의 매수에 쓰는 금액 상한(KRW)입니다. 현금이 더 많아도 이 금액까지만 삽니다.\n체크를 풀면 상한이 없습니다.",
    max_position_ratio: "보유 코인 평가액 합이 총자산(현금+코인)의 이 퍼센트를 넘지 않게 매수 예산을 줄입니다.\n100% = 제한 없음, 50% = 자산의 절반까지만 코인 보유.",
    max_open_positions: "동시에 보유할 수 있는 마켓 수입니다. 1이면 한 코인을 들고 있는 동안 다른 코인은 사지 않습니다.\n같은 코인 추가 매수는 하지 않습니다.",
    daily_loss_limit_pct: "당일(한국 시간 자정 기준) 시작 자산 대비 평가 손실이 이 퍼센트에 닿으면(3 = -3%) 그날은 신규 매수를 멈춥니다.\n손절·매도는 계속되고 다음 날 자동으로 풀립니다. 대시보드로 옮긴 입출금은 손실로 치지 않습니다. 체크를 풀면 끕니다.",
    max_consecutive_losses: "매도로 확정된 손실 거래가 연속 이 횟수면 그날 신규 매수를 멈춥니다.\n이익 거래가 한 번 나오면 0으로 돌아가고, 다음 날 풀립니다. 체크를 풀면 끕니다.",
    stop_loss_pct: "평균 매수가보다 이 퍼센트만큼 내려가면(5 = -5%) 캔들을 기다리지 않고 실시간 시세로 시장가 매도합니다.\n1초마다 감시하고, 튄 시세 한 번으로 팔지 않도록 연속 두 번 확인한 뒤 실행합니다. 체크를 풀면 끕니다.",
    take_profit_pct: "평균 매수가보다 이 퍼센트만큼 오르면(10 = +10%) 매도합니다.\n체크를 풀면 데드크로스(전략 매도 신호)까지 보유합니다.",
    trailing_stop_pct: "매수 후 최고가를 기억했다가 거기서 이 퍼센트만큼 떨어지면 매도합니다(10 = 고점 대비 -10%).\n오른 만큼의 이익을 지키는 용도입니다. 체크를 풀면 끕니다.",
    price_deviation_limit: "신호 캔들 종가와 실제 주문 순간 현재가의 차이가 이 퍼센트(10 = 10%)를 넘으면 매수하지 않습니다.\n정체 뒤 뒤늦은 진입이나 급등 추격을 막습니다. 체크를 풀면 끕니다.",
    min_order_amount: "계산된 매수 예산이 이 금액(KRW)보다 작으면 사지 않습니다.\n업비트 원화 마켓 최소 주문은 5,000원이라 더 낮추면 거래소가 거부합니다.",
    cooldown_seconds: "한 마켓을 청산(매도·손절)한 뒤 이 시간(초) 동안은 같은 마켓을 다시 사지 않습니다. 0 = 바로 가능.",
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
        logFilter: { from: "", to: "", q: "" }, logsHasMore: false, logsLoading: false,
        meta: { available: {}, schemas: {}, intervals: [], risk_schema: {} },
        settingsVersion: 0, settingsHistory: [], form: null, saving: false, saveResult: null, formErrors: [],
        pockets: {}, transfer: { direction: "to_main", amount: 0, bot_pocket_uuid: null }, transferResult: null,
        showDust: false,
        // 코인 선택 팝업 (설정 탭)
        picker: { open: false, loading: false, error: null, data: null, query: "", onlySelected: false, selected: new Set(), target: "settings",
          sort: { key: "acc_trade_price_24h", desc: true } },
        marketCatalog: {},  // 코드 → 한글 이름
        loadedVersion: null,  // 이력에서 폼에 불러온 버전
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
      riskFields() {
        return schemaFields(this.meta.risk_schema).map((f) => PERCENT_FIELDS.has(f.name)
          ? Object.assign({}, f, { percent: true, min: 0, max: 100, step: 0.1, desc: "0~100" })
          : f);
      },
    },
    methods: {
      // ---------- 유틸
      krw(v) { return v == null || isNaN(v) ? "-" : Math.round(v).toLocaleString("ko-KR") + " 원"; },
      num(v, d) { return v == null || isNaN(v) ? "-" : Number(v).toLocaleString("ko-KR", { maximumFractionDigits: d, minimumFractionDigits: d > 4 ? d : 0 }); },
      pct(v, d) { if (v == null || isNaN(v)) return "-"; const p = v * 100; return (p > 0 ? "+" : "") + p.toFixed(d == null ? 2 : d) + "%"; },
      sign(v) { return v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : ""; },
      fmtTime(iso) { if (!iso) return "-"; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString("ko-KR", { hour12: false }); },
      riskLabel(name) { return RISK_LABELS[name] || name; },
      help(name) { return HELP[name] || ""; },
      clampPercent(f) {
        // 비율 항목은 0~100 밖의 값을 받지 않는다 (입력 즉시 되돌림)
        if (!f.percent) return;
        const v = this.form.risk[f.name];
        if (v === "" || v == null) return;
        if (v > 100) this.form.risk[f.name] = 100;
        else if (v < 0) this.form.risk[f.name] = 0;
      },
      riskPayload() {
        // 화면(0~100 %) → API(0~1 소수). 켜져 있는 비율 항목이 0~100 을 벗어나거나 0 이면 저장하지 않는다
        const risk = {};
        const problems = [];
        for (const f of this.riskFields) {
          const raw = this.form.risk[f.name];
          const off = f.nullable && !this.form.riskEnabled[f.name];
          if (off || raw === "" || raw == null) { risk[f.name] = null; continue; }
          if (f.percent) {
            const v = Number(raw);
            if (!(v >= 0 && v <= 100)) problems.push(`${this.riskLabel(f.name)}: 0~100 사이로 입력하세요 (입력값 ${raw})`);
            else if (v === 0 && f.nullable) problems.push(`${this.riskLabel(f.name)}: 0 은 쓸 수 없습니다. 끄려면 체크를 해제하세요`);
            else if (v === 0) problems.push(`${this.riskLabel(f.name)}: 0 보다 커야 합니다`);
            risk[f.name] = fromPercent(v);
          } else {
            risk[f.name] = raw;
          }
        }
        if (problems.length) throw new Error(problems.join("; "));
        return risk;
      },
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
          dashboard_kill: "강제 종료", pocket_transfer: "포켓 이전", notify_test: "알림 테스트", notify_failed: "알림 실패", account_sync: "잔고 확인 필요", order_filled: "체결",
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
          this.fillForm(s.data, "");
          this.loadedVersion = null;
          if (!this.picker.data && !this.picker.loading) this.loadMarketCatalog();  // 선택된 마켓의 한글 이름 표시용
          this.syncBacktestFromSettings();
          if (!this.btDefaults.years.length) this.loadBacktestDefaults();
        } catch (e) { this.notify("설정 조회 실패: " + e.message, "bad"); }
      },
      fillForm(d, note) {
        const riskEnabled = {};
        for (const f of schemaFields(this.meta.risk_schema)) riskEnabled[f.name] = d.risk[f.name] != null;
        const risk = Object.assign({}, d.risk);
        for (const k of Object.keys(risk)) if (PERCENT_FIELDS.has(k)) risk[k] = toPercent(risk[k]);  // 0.05 → 5 (%)
        this.form = { marketsText: d.markets.join(","), candle_interval: d.candle_interval, strategy_name: d.strategy_name,
          strategy_params: Object.assign({}, d.strategy_params), risk, riskEnabled, note: note || "" };
        // 파라미터 기본값 채우기
        for (const f of schemaFields(this.meta.schemas[d.strategy_name])) if (this.form.strategy_params[f.name] == null) this.form.strategy_params[f.name] = f.default;
        this.saveResult = null; this.formErrors = [];
      },
      async loadSettingsVersion(v) {
        try {
          const s = await this.api(`/api/settings/${v}`);
          this.fillForm(s.data, `v${v} 설정 복원`); this.loadedVersion = v;
          this.notify(`v${v} 설정을 불러왔습니다. 저장을 누르면 새 버전으로 적용됩니다`, "ok");
        } catch (e) { this.notify("불러오기 실패: " + e.message, "bad"); }
      },
      async restoreSettingsVersion(v) {
        if (!confirm(`v${v} 설정을 새 버전으로 바로 저장합니다. 계속할까요?`)) return;
        try {
          const s = await this.api(`/api/settings/${v}`);
          const r = await this.api("/api/settings", { method: "PUT", body: { data: s.data, note: `v${v} 설정 복원` } });
          await this.loadSettings();
          this.saveResult = r;
          this.notify(`v${r.version} 저장됨 (v${v} 복원)`, "ok");
        } catch (e) { this.notify("되돌리기 실패: " + e.message, "bad"); }
      },
      resetParams() {
        const params = {};
        for (const f of schemaFields(this.meta.schemas[this.form.strategy_name])) params[f.name] = f.default;
        this.form.strategy_params = params;
      },
      async saveSettings() {
        if (!this.form) return;
        let risk;
        try { risk = this.riskPayload(); } catch (e) { this.formErrors = e.message.split("; "); this.notify("입력값을 확인하세요", "bad"); return; }
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
          if (!this.bt.job && this.bt.jobs.length) this.loadBacktestJob(this.bt.jobs[0].id);  // 마지막 결과를 바로 보여준다
        } catch (e) { /* noop */ }
      },
      syncBacktestFromSettings() {
        if (!this.form) return;
        if (!this.bt.marketsText) this.bt.marketsText = this.form.marketsText;
        if (!this.bt.interval) this.bt.interval = this.form.candle_interval;
      },
      async runBacktest() {
        if (!this.form) return;
        let risk = null;
        if (this.bt.useRisk) {
          try { risk = this.riskPayload(); } catch (e) { this.bt.error = e.message; this.notify("리스크 입력값을 확인하세요", "bad"); return; }
        }
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
        try { await this.api(`/api/backtest/jobs/${this.bt.job.id}/cancel`, { method: "POST", body: {} }); } catch (e) { this.notify("중단 실패: " + e.message, "bad"); }
      },
      async deleteBacktestJob(id) {
        if (!confirm("이 백테스트 결과를 삭제합니다. 계속할까요?")) return;
        try {
          await this.api(`/api/backtest/jobs/${id}`, { method: "DELETE" });
          this.bt.jobs = await this.api("/api/backtest/jobs");
          if (this.bt.job && this.bt.job.id === id) { this.bt.job = null; this.bt.selected = null; }
          this.bt.selectedJobId = "";
          this.notify("백테스트 결과를 삭제했습니다", "ok");
        } catch (e) { this.notify("삭제 실패: " + e.message, "bad"); }
      },
      async loadBacktestJob(id) {
        if (!id) return;
        try {
          this.bt.job = await this.api(`/api/backtest/jobs/${id}`); this.bt.selected = null; this.bt.selectedJobId = id;
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
      // ---------- 로그: 최근 100개 + 이전 페이지(before_id) + 날짜·레벨·검색 필터
      logQuery(beforeId) {
        const p = new URLSearchParams({ limit: "100" });
        if (this.logLevel) p.set("level", this.logLevel);
        if (this.logFilter.from) p.set("date_from", this.logFilter.from);
        if (this.logFilter.to) p.set("date_to", this.logFilter.to);
        if (this.logFilter.q) p.set("q", this.logFilter.q);
        if (beforeId) p.set("before_id", String(beforeId));
        return `/api/logs?${p.toString()}`;
      },
      logFilterActive() { return !!(this.logFilter.from || this.logFilter.to || this.logFilter.q); },
      async loadLogs() {
        this.logsLoading = true;
        try { const r = await this.api(this.logQuery()); this.logs = r.items; this.logsHasMore = r.has_more; }
        catch (e) { this.notify("로그 조회 실패: " + e.message, "bad"); }
        this.logsLoading = false;
      },
      async loadMoreLogs() {
        if (this.logsLoading || !this.logsHasMore || !this.logs.length) return;
        this.logsLoading = true;
        try {
          const r = await this.api(this.logQuery(this.logs[this.logs.length - 1].id));
          this.logs = this.logs.concat(r.items); this.logsHasMore = r.has_more;
        } catch (e) { this.notify("로그 조회 실패: " + e.message, "bad"); }
        this.logsLoading = false;
      },
      onLogScroll(ev) { const el = ev.target; if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) this.loadMoreLogs(); },
      resetLogFilter() { this.logFilter = { from: "", to: "", q: "" }; this.logLevel = ""; this.loadLogs(); },
      // ---------- 실시간
      connectWs() {
        if (this.ws) { try { this.ws.close(); } catch (e) { /* noop */ } this.ws = null; }
        const proto = location.protocol === "https:" ? "wss" : "ws";
        const ws = new WebSocket(`${proto}://${location.host}/ws?mode=${this.mode}`);
        // 토큰은 URL 이 아니라 접속 직후 첫 메시지로 보낸다 (서버 로그·프록시에 남지 않게)
        ws.onopen = () => { if (this.token) ws.send(JSON.stringify({ token: this.token })); this.wsConnected = true; };
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
      // 로그 자동 갱신은 첫 페이지·필터 없음일 때만 (더 보기로 내려간 상태를 덮어쓰지 않도록)
      this.timers.push(setInterval(() => { if (this.tab === "logs" && this.logs.length <= 100 && !this.logFilterActive()) this.loadLogs(); }, 15000));
    },
  }).mount("#app");
})();
