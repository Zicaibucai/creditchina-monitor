import {
  Activity,
  Bell,
  Building2,
  CalendarClock,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Clock3,
  Database,
  Download,
  Eye,
  FileCheck2,
  FileCode2,
  Gauge,
  History,
  Images,
  LayoutDashboard,
  LoaderCircle,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Settings,
  ShieldCheck,
  Square,
  Sparkles,
  Trash2,
  X,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { crawlerApiJson, crawlerAssetUrl, downloadCompanyEvidencePackage, downloadEvidencePackage, downloadRealWorkbook } from "./crawler-api";
import SettingsView from "./settings-view";

type Company = {
  id: number;
  name: string;
  code: string;
  legalPerson: string;
  status: string;
  permission: number;
  penalty: number;
  updated: string;
  region: string;
  creditScore: number | null;
  creditScoreDate: string;
  creditScoreUpdated: string;
};

type Announcement = {
  id: number;
  company: string;
  section: string;
  count: number;
  summary: string;
  type: "added" | "deleted" | "modified";
  recordKey: string;
  hasBeforeEvidence: boolean;
  hasAfterEvidence: boolean;
  createdAt: string;
};

type Task = {
  id: string;
  name: string;
  progress: number;
  completed: number;
  total: number;
  status: string;
  speed: string;
  currentCompany?: string;
  error?: string;
};

type DashboardPayload = {
  scope: string;
  configuredCompanies: string[];
  configuredCount: number;
  collectedCount: number;
  creditScoreCollectedCount: number;
  companies: Company[];
  announcements: Announcement[];
  activeTask: Task | null;
  lastTask: Task | null;
  creditScoreTask: Task | null;
  nextRun: string;
  autoDaily: boolean;
  intervalSeconds: number;
  proxyMode: string;
  proxyReplacementLimit: number;
  updatedAt: string;
};

type CompanyDetail = {
  name: string;
  basic: Record<string, unknown>;
  permissions: Record<string, unknown>[];
  penalties: Record<string, unknown>[];
  errors: Record<string, string>;
  evidence: EvidenceCapture[];
  creditScore: CreditScore | null;
};

type CreditScore = {
  scoreTotal: number | null;
  scoreBasic?: number | null;
  scoreAchievement?: number | null;
  scoreSafetystandards?: number | null;
  scoreAward?: number | null;
  scoreBlxyf?: number | null;
  scoreOther?: number | null;
  reportDate?: string;
  collectedAt?: string;
};

type EvidenceItem = {
  id: number;
  identity: string;
  documentNumber: string;
  hasImage: boolean;
};

type EvidenceCapture = {
  id: number;
  company: string;
  capturedAt: string;
  penaltyCount: number;
  sourceUrl: string;
  hasOverview: boolean;
  hasPanel: boolean;
  hasHtml: boolean;
  hasMetadata: boolean;
  items: EvidenceItem[];
};

type View = "overview" | "companies" | "announcements" | "settings";

const emptyDashboard: DashboardPayload = {
  scope: "行政管理（行政许可 + 行政处罚）",
  configuredCompanies: [],
  configuredCount: 0,
  collectedCount: 0,
  creditScoreCollectedCount: 0,
  companies: [],
  announcements: [],
  activeTask: null,
  lastTask: null,
  creditScoreTask: null,
  nextRun: "--",
  autoDaily: false,
  intervalSeconds: 0,
  proxyMode: "直连/静态代理",
  proxyReplacementLimit: 20,
  updatedAt: "--",
};

export default function Home() {
  const [dashboard, setDashboard] = useState<DashboardPayload>(emptyDashboard);
  const [view, setView] = useState<View>("overview");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Company | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<CompanyDetail | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [creditRunning, setCreditRunning] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [evidence, setEvidence] = useState<{ url: string; title: string } | null>(null);
  const [toast, setToast] = useState("");
  const [rowAction, setRowAction] = useState("");
  const [confirmRemove, setConfirmRemove] = useState("");

  const refresh = useCallback(async () => {
    try {
      const payload = await crawlerApiJson<DashboardPayload>("/monitor/dashboard");
      setDashboard(payload);
      setError("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "本机采集服务连接失败");
    }
  }, []);

  useEffect(() => {
    const initialRefresh = window.setTimeout(() => void refresh(), 0);
    const timer = window.setInterval(refresh, 10000);
    return () => {
      window.clearTimeout(initialRefresh);
      window.clearInterval(timer);
    };
  }, [refresh]);

  useEffect(() => {
    if (!selected) return;
    let active = true;
    void crawlerApiJson<CompanyDetail>(`/monitor/companies/${encodeURIComponent(selected.name)}`)
      .then((detail) => active && setSelectedDetail(detail))
      .catch(() => active && setSelectedDetail(null));
    return () => {
      active = false;
    };
  }, [selected]);

  const selectCompany = (company: Company) => {
    setSelectedDetail(null);
    setSelected(company);
  };

  const companies = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return dashboard.companies;
    return dashboard.companies.filter((company) =>
      company.name.toLowerCase().includes(normalized) || company.code.toLowerCase().includes(normalized),
    );
  }, [dashboard.companies, query]);

  const permissionTotal = dashboard.companies.reduce((sum, company) => sum + company.permission, 0);
  const penaltyTotal = dashboard.companies.reduce((sum, company) => sum + company.penalty, 0);
  const today = new Date().toISOString().slice(0, 10);
  const todayUpdates = dashboard.announcements.filter((item) => item.createdAt.startsWith(today)).length;
  const hasActiveCrawler = !!dashboard.activeTask || ["queued", "running"].includes(dashboard.creditScoreTask?.status || "");

  const flash = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 2600);
  }, []);

  const runNow = async () => {
    setRunning(true);
    try {
      await crawlerApiJson("/monitor/run", { method: "POST" });
      flash(`${dashboard.configuredCount} 家企业行政管理复查已加入队列`);
      await refresh();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "启动失败");
    } finally {
      setRunning(false);
    }
  };

  const resumeLastTask = async () => {
    if (!dashboard.lastTask) return;
    setResuming(true);
    try {
      await crawlerApiJson(`/tasks/${dashboard.lastTask.id}`, {
        method: "PATCH",
        body: JSON.stringify({ action: "resume" }),
      });
      flash(`已从 ${dashboard.lastTask.completed}/${dashboard.lastTask.total} 家的断点继续`);
      await refresh();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "断点恢复失败");
    } finally {
      setResuming(false);
    }
  };

  const runCreditScores = async () => {
    setCreditRunning(true);
    try {
      await crawlerApiJson("/monitor/credit-scores/run", { method: "POST" });
      flash(`${dashboard.configuredCount} 家企业信用分采集已开始`);
      await refresh();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "信用分采集启动失败");
    } finally {
      setCreditRunning(false);
    }
  };

  const stopAllCrawlers = async () => {
    setStopping(true);
    try {
      const result = await crawlerApiJson<{ stopped: boolean; taskCount: number; creditScoreCount: number }>(
        "/monitor/stop",
        { method: "POST" },
      );
      const count = result.taskCount + result.creditScoreCount;
      flash(count ? `已停止 ${count} 个采集任务` : "当前没有正在运行的采集任务");
      await refresh();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "停止采集失败");
    } finally {
      setStopping(false);
    }
  };

  const exportWorkbook = async (mode: "penalties" | "all", company = "") => {
    try {
      const filename = await downloadRealWorkbook(mode, company);
      flash(`已下载：${filename}`);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "导出失败");
    }
  };

  const runSingleCompany = async (name: string) => {
    setRowAction(`crawl:${name}`);
    try {
      await crawlerApiJson(`/monitor/companies/${encodeURIComponent(name)}/run`, { method: "POST" });
      flash(`已启动「${name}」定向采集`);
      await refresh();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "定向采集启动失败");
    } finally {
      setRowAction("");
    }
  };

  const runSingleCreditScore = async (name: string) => {
    setRowAction(`score:${name}`);
    try {
      await crawlerApiJson(`/monitor/companies/${encodeURIComponent(name)}/credit-score/run`, { method: "POST" });
      flash(`已启动「${name}」信用分采集`);
      await refresh();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "信用分采集启动失败");
    } finally {
      setRowAction("");
    }
  };

  const removeCompany = async (name: string) => {
    setRowAction(`remove:${name}`);
    try {
      const result = await crawlerApiJson<{ count: number }>(
        `/monitor/companies/${encodeURIComponent(name)}`,
        { method: "DELETE" },
      );
      flash(`已从固定名单移除「${name}」，当前 ${result.count} 家`);
      setConfirmRemove("");
      if (selected?.name === name) setSelected(null);
      await refresh();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "移除失败");
    } finally {
      setRowAction("");
    }
  };

  const addCompanies = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const names = String(form.get("companies") || "").split(/\n|,|，/).map((name) => name.trim()).filter(Boolean);
    setAdding(true);
    try {
      const result = await crawlerApiJson<{ added: number; count: number }>("/monitor/companies", {
        method: "POST",
        body: JSON.stringify({ companies: names }),
      });
      setAddOpen(false);
      flash(`已新增 ${result.added} 家，当前固定名单 ${result.count} 家`);
      await refresh();
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "添加企业失败");
    } finally {
      setAdding(false);
    }
  };

  const nav = [
    { id: "overview" as const, label: "监控总览", icon: LayoutDashboard },
    { id: "companies" as const, label: "企业清单", icon: Building2 },
    { id: "announcements" as const, label: "更新公告", icon: Bell },
    { id: "settings" as const, label: "系统设置", icon: Settings },
  ];

  const openEvidence = (item: Announcement, which: "before" | "after") => {
    setEvidence({
      url: crawlerAssetUrl(`/monitor/announcements/${item.id}/evidence/${which}?t=${Date.now()}`),
      title: `${item.company} · ${which === "before" ? "变更前证据" : "变更后证据"}`,
    });
  };

  const downloadPackage = async (item: Announcement) => {
    try {
      const filename = await downloadEvidencePackage(item.id);
      flash(`已下载证据包：${filename}`);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "证据包下载失败");
    }
  };

  const openCompanyEvidence = (
    capture: EvidenceCapture,
    asset: "overview" | "panel" | "item",
    item?: EvidenceItem,
  ) => {
    const url = asset === "item" && item
      ? crawlerAssetUrl(`/monitor/evidence/${capture.id}/items/${item.id}?t=${Date.now()}`)
      : crawlerAssetUrl(`/monitor/evidence/${capture.id}/assets/${asset}?t=${Date.now()}`);
    const label = asset === "overview" ? "官网整页" : asset === "panel" ? "行政处罚栏目" : item?.documentNumber || "行政处罚";
    setEvidence({ url, title: `${capture.company} · ${label}` });
  };

  const downloadCompanyPackage = async (capture: EvidenceCapture) => {
    try {
      const filename = await downloadCompanyEvidencePackage(capture.id);
      flash(`已下载证据包：${filename}`);
    } catch (caught) {
      flash(caught instanceof Error ? caught.message : "证据包下载失败");
    }
  };

  return (
    <main className="monitor-shell">
      <aside className="monitor-sidebar">
        <div className="monitor-brand"><span><ShieldCheck size={23} /></span><div><strong>中建探员</strong><small>信息中国信息采集</small></div></div>
        <nav>
          {nav.map((item) => {
            const Icon = item.icon;
            return <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}><Icon size={18} />{item.label}{item.id === "announcements" && todayUpdates > 0 && <b>{todayUpdates}</b>}</button>;
          })}
        </nav>
        <div className="monitor-scope-card"><Sparkles size={18} /><span><small>当前采集范围</small><strong>行政许可 + 行政处罚</strong><em>其他信用栏目已关闭</em></span></div>
        <div className="monitor-service"><i className={error ? "offline" : ""} /><span><strong>{error ? "服务未连接" : "本机服务在线"}</strong><small>{error ? "请启动 api_server.py" : "手动整轮更新模式"}</small></span></div>
      </aside>

      <section className="monitor-main">
        <header className="monitor-topbar">
          <div><span>固定企业行政管理监控</span><strong>{dashboard.configuredCount || 27} 家企业池</strong></div>
          <div className="monitor-top-actions">
            <span className="freshness"><RefreshCw size={14} />数据时间 {dashboard.updatedAt}</span>
            <button className="monitor-secondary" onClick={() => setAddOpen(true)}><Plus size={16} />添加企业</button>
            <button className="monitor-secondary" onClick={() => void refresh()}><RefreshCw size={16} />刷新看板</button>
            <button className="monitor-secondary monitor-score-button" disabled={creditRunning || ["queued", "running"].includes(dashboard.creditScoreTask?.status || "")} onClick={runCreditScores}>{creditRunning || ["queued", "running"].includes(dashboard.creditScoreTask?.status || "") ? <LoaderCircle className="spin" size={16} /> : <Gauge size={16} />}{["queued", "running"].includes(dashboard.creditScoreTask?.status || "") ? `信用分 ${dashboard.creditScoreTask?.completed || 0}/${dashboard.creditScoreTask?.total || dashboard.configuredCount}` : "采集信用分"}</button>
            <button className="monitor-stop-button" disabled={stopping || !hasActiveCrawler} onClick={() => void stopAllCrawlers()}>{stopping ? <LoaderCircle className="spin" size={16} /> : <Square size={14} fill="currentColor" />}停止全部</button>
            <button className="monitor-primary" disabled={running || !!dashboard.activeTask} onClick={runNow}>{running ? <LoaderCircle className="spin" size={16} /> : <Activity size={16} />}手动更新一轮</button>
          </div>
        </header>

        <div className="monitor-content">
          {error && <div className="monitor-error"><CircleAlert size={18} />{error}</div>}
          {dashboard.configuredCount < 27 && <div className="monitor-setup"><FileCheck2 size={20} /><div><strong>当前已录入 {dashboard.configuredCount} 家，目标 27 家</strong><p>本次收到 14 家企业，剩余企业可点击“添加企业”动态补充。</p></div></div>}

          {view === "overview" && <>
            <section className="monitor-hero">
              <div><span><Sparkles size={14} />行政处罚证据监控</span><h1>固定企业，一张看板<br />掌握每轮发生的变化</h1><p>一个代理 IP 会在同一 Chrome/CDP 会话中连续采集多家企业，直到代理断线或官网返回 403/412/429。程序会保留已完成公司和当前采集阶段的断点，然后换 IP 继续；只有用尽更换次数才停止整轮。</p></div>
              <div className="monitor-orbit"><span><ShieldCheck size={34} /></span><i /><i /><i /><b>DAILY</b></div>
            </section>

            <section className="monitor-metrics">
              <Metric icon={Building2} label="固定企业" value={dashboard.configuredCount} note={`已完成 ${dashboard.collectedCount} 家`} tone="blue" />
              <Metric icon={FileCheck2} label="行政许可记录" value={permissionTotal} note="当前最新快照" tone="cyan" />
              <Metric icon={CircleAlert} label="行政处罚记录" value={penaltyTotal} note={`${dashboard.companies.filter((item) => item.penalty > 0).length} 家涉及处罚`} tone="orange" />
              <Metric icon={Bell} label="今日更新公告" value={todayUpdates} note="新增或内容变更" tone="violet" />
            </section>

            <section className="monitor-grid">
              <article className="monitor-panel update-board">
                <PanelTitle eyebrow="CHANGE FEED" title="企业更新公告" action={<button onClick={() => setView("announcements")}>查看全部<ChevronRight size={15} /></button>} />
                <AnnouncementList items={dashboard.announcements.slice(0, 6)} onEvidence={openEvidence} onPackage={downloadPackage} />
              </article>
              <article className="monitor-panel schedule-panel">
                <PanelTitle eyebrow="SCHEDULE" title="每日采集计划" />
                <div className="schedule-line"><CalendarClock size={20} /><span><small>执行方式</small><strong>{dashboard.autoDaily ? `下次 ${dashboard.nextRun}` : "仅点击按钮后执行一轮"}</strong></span></div>
                <div className="schedule-line"><Clock3 size={20} /><span><small>官网请求节奏</small><strong>{dashboard.intervalSeconds === 0 ? "连续请求（无等待）" : `至少 ${dashboard.intervalSeconds} 秒 / 次`}</strong></span></div>
                <div className="schedule-line"><ShieldCheck size={20} /><span><small>出口策略</small><strong>{dashboard.proxyMode}</strong></span></div>
                <div className="schedule-line"><Database size={20} /><span><small>采集栏目</small><strong>{dashboard.scope}</strong></span></div>
                {dashboard.activeTask ? <div className="active-run"><div><span>正在执行</span><strong>{dashboard.activeTask.currentCompany || dashboard.activeTask.name}</strong><em>{dashboard.activeTask.completed}/{dashboard.activeTask.total} 家</em></div><i><b style={{ width: `${dashboard.activeTask.progress}%` }} /></i><small>{dashboard.activeTask.speed}</small></div> : dashboard.lastTask && ["intercepted", "failed"].includes(dashboard.lastTask.status) ? <div className="intercepted-run"><CircleAlert size={18} /><span><strong>上轮已停在 {dashboard.lastTask.completed}/{dashboard.lastTask.total} 家断点</strong><small>{dashboard.lastTask.error}</small><button disabled={resuming} onClick={() => void resumeLastTask()}>{resuming ? <LoaderCircle className="spin" size={14} /> : <RotateCcw size={14} />}从原任务断点继续</button></span></div> : <div className="idle-run"><CheckCircle2 size={18} />当前无运行任务</div>}
              </article>
            </section>

            <article className="monitor-panel company-snapshot">
              <PanelTitle eyebrow="COMPANY SNAPSHOT" title="企业状态速览" action={<button onClick={() => setView("companies")}>全部企业<ChevronRight size={15} /></button>} />
              <CompanyTable companies={dashboard.companies.slice(0, 8)} onSelect={selectCompany} />
            </article>
          </>}

          {view === "companies" && <article className="monitor-panel company-page">
            <PanelTitle eyebrow="MONITORED COMPANIES" title={`固定企业清单 · ${dashboard.configuredCount} 家`} action={<div className="company-filter"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选企业名称或信用代码" /></div>} />
            <CompanyTable
              companies={companies}
              onSelect={selectCompany}
              onRun={runSingleCompany}
              onCreditScore={runSingleCreditScore}
              onRemove={removeCompany}
              rowAction={rowAction}
              confirmRemove={confirmRemove}
              onConfirmRemove={setConfirmRemove}
              busy={!!dashboard.activeTask}
            />
          </article>}

          {view === "announcements" && <article className="monitor-panel announcement-page">
            <PanelTitle eyebrow="UPDATE BULLETIN" title="更新公告栏" action={<button onClick={() => void exportWorkbook("all")}><Download size={15} />导出历史全部信息</button>} />
            <p className="board-note">仅展示第二次及后续复查发现的新增或内容变更。官网已删除的旧记录不会从历史仓中移除。</p>
            <AnnouncementList items={dashboard.announcements} onEvidence={openEvidence} onPackage={downloadPackage} />
          </article>}

          {/* 设置页始终挂载，切换视图时仅隐藏：避免 10 秒定时刷新
              卸载组件导致表单输入丢失。 */}
          <div style={{ display: view === "settings" ? "block" : "none" }}>
            <SettingsView
              companies={dashboard.configuredCompanies}
              onCompaniesChanged={refresh}
              onToast={flash}
            />
          </div>
        </div>
      </section>

      {addOpen && <div className="company-modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setAddOpen(false)}><form className="add-company-modal" onSubmit={addCompanies}><button type="button" className="modal-close" onClick={() => setAddOpen(false)}><X size={19} /></button><span>FIXED COMPANY POOL</span><h2>动态添加监控企业</h2><p>每行一家，也可以使用逗号分隔。重复名称会自动忽略。</p><textarea name="companies" required autoFocus placeholder={'示例企业一有限公司\n示例企业二有限公司'} /><div><button type="button" onClick={() => setAddOpen(false)}>取消</button><button type="submit" disabled={adding}>{adding ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />}添加到固定名单</button></div></form></div>}
      {selected && <div className="company-modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setSelected(null)}>
        <article className="company-modal">
          <button className="modal-close" onClick={() => setSelected(null)}><X size={19} /></button>
          <div className="company-modal-head"><span>{selected.name.charAt(0)}</span><div><small>{selected.region} · {selected.status}</small><h2>{selected.name}</h2><code>{selected.code || "统一社会信用代码待采集"}</code></div></div>
          <div className="company-info-grid"><div><small>法定代表人</small><strong>{selected.legalPerson || "--"}</strong></div><div><small>最后更新</small><strong>{selected.updated}</strong></div><div><small>行政许可</small><strong>{selected.permission}</strong></div><div className={selected.penalty ? "risk" : ""}><small>行政处罚</small><strong>{selected.penalty}</strong></div><div className="credit-score-card"><small>上海住建信用分</small><strong>{formatCreditScore(selected.creditScore)}</strong><em>{selected.creditScoreDate || "尚未采集"}</em></div></div>
          {selectedDetail && <>
            <div className="company-record-sections"><RecordSection title="行政许可" items={selectedDetail.permissions} kind="permission" /><RecordSection title="行政处罚" items={selectedDetail.penalties} kind="penalty" /></div>
            <EvidenceCaptureList captures={selectedDetail.evidence || []} onOpen={openCompanyEvidence} onPackage={downloadCompanyPackage} />
          </>}
          <div className="company-modal-actions"><button className="primary-action" disabled={rowAction === `crawl:${selected.name}` || !!dashboard.activeTask} onClick={() => void runSingleCompany(selected.name)}>{rowAction === `crawl:${selected.name}` ? <LoaderCircle className="spin" size={16} /> : <Zap size={16} />}定向采集该企业</button><button onClick={() => void exportWorkbook("all", selected.name)}><Download size={16} />导出该企业历史</button><button className="danger-export" onClick={() => void exportWorkbook("penalties", selected.name)}><History size={16} />导出行政处罚</button></div>
        </article>
      </div>}
      {evidence && <div className="evidence-modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setEvidence(null)}><article className="evidence-modal"><header><div><span>OFFICIAL EVIDENCE</span><h2>{evidence.title}</h2><p>图片来自信用中国官网原始 DOM，证据目录同时保存源码、JSON、时间和 SHA-256。</p></div><button onClick={() => setEvidence(null)}><X size={19} /></button></header><div><img src={evidence.url} alt={evidence.title} /></div><footer><a href={evidence.url} target="_blank" rel="noreferrer"><Download size={15} />打开原图</a></footer></article></div>}
      {toast && <div className="monitor-toast"><CheckCircle2 size={17} />{toast}</div>}
    </main>
  );
}

function Metric({ icon: Icon, label, value, note, tone }: { icon: typeof Building2; label: string; value: number; note: string; tone: string }) {
  return <article className={`monitor-metric ${tone}`}><span><Icon size={21} /></span><div><small>{label}</small><strong>{value.toLocaleString("zh-CN")}</strong><em>{note}</em></div></article>;
}

function PanelTitle({ eyebrow, title, action }: { eyebrow: string; title: string; action?: React.ReactNode }) {
  return <header className="monitor-panel-title"><div><span>{eyebrow}</span><h2>{title}</h2></div>{action}</header>;
}

function AnnouncementList({ items, onEvidence, onPackage }: { items: Announcement[]; onEvidence: (item: Announcement, which: "before" | "after") => void; onPackage: (item: Announcement) => void }) {
  if (!items.length) return <div className="monitor-empty"><Bell size={24} /><strong>暂无更新公告</strong><span>完成第二轮复查后，新增或变更会出现在这里。</span></div>;
  const labels = { added: "新增", deleted: "删除", modified: "变更" };
  return <div className="announcement-list">{items.map((item) => <div key={item.id} className={`announcement-item ${item.type || "added"}`}><span>{item.type === "deleted" ? <History size={17} /> : item.type === "modified" ? <RefreshCw size={17} /> : <CircleAlert size={17} />}</span><div><strong>{item.company}</strong><p>{item.summary}</p><small>{item.createdAt}{item.recordKey ? ` · ${item.recordKey}` : ""}</small><div className="evidence-actions">{item.hasBeforeEvidence && <button onClick={() => onEvidence(item, "before")}>变更前截图</button>}{item.hasAfterEvidence && <button onClick={() => onEvidence(item, "after")}>{item.type === "deleted" ? "删除后整页" : "官网截图"}</button>}<button onClick={() => onPackage(item)}>下载证据包</button></div></div><b>{labels[item.type] || "+"}</b></div>)}</div>;
}

function CompanyTable({
  companies,
  onSelect,
  onRun,
  onCreditScore,
  onRemove,
  rowAction = "",
  confirmRemove = "",
  onConfirmRemove,
  busy = false,
}: {
  companies: Company[];
  onSelect: (company: Company) => void;
  onRun?: (name: string) => void;
  onCreditScore?: (name: string) => void;
  onRemove?: (name: string) => void;
  rowAction?: string;
  confirmRemove?: string;
  onConfirmRemove?: (name: string) => void;
  busy?: boolean;
}) {
  if (!companies.length) return <div className="monitor-empty"><Building2 size={24} /><strong>暂无企业数据</strong><span>录入固定名单并完成首次采集后显示。</span></div>;
  const manageable = !!(onRun && onRemove);
  return <div className="monitor-table-wrap"><table className="monitor-table"><thead><tr><th>企业</th><th>信用分</th><th>状态</th><th>法定代表人</th><th>行政许可</th><th>行政处罚</th><th>最后更新</th>{manageable && <th className="actions-col">操作</th>}<th /></tr></thead><tbody>{companies.map((company) => <tr key={company.name} onClick={() => onSelect(company)}><td><span className="company-avatar">{company.name.charAt(0)}</span><div><strong>{company.name}</strong><code>{company.code || "待采集"}</code></div></td><td><span className={`credit-score-pill ${company.creditScore == null ? "empty" : ""}`}><Gauge size={13} />{formatCreditScore(company.creditScore)}</span><small className="credit-score-date">{company.creditScoreDate || "待采集"}</small></td><td><em className="normal-status"><i />{company.status}</em></td><td>{company.legalPerson || "--"}</td><td><b className="count-pill permit">{company.permission}</b></td><td><b className={`count-pill ${company.penalty ? "penalty" : ""}`}>{company.penalty}</b></td><td>{company.updated}</td>{manageable && <td className="company-row-actions" onClick={(event) => event.stopPropagation()}>
    {confirmRemove === company.name ? (
      <span className="row-confirm">
        移除？
        <button className="row-confirm-yes" disabled={rowAction === `remove:${company.name}`} onClick={() => onRemove(company.name)}>{rowAction === `remove:${company.name}` ? <LoaderCircle className="spin" size={12} /> : <CheckCircle2 size={12} />}确认</button>
        <button className="row-confirm-no" onClick={() => onConfirmRemove?.("")}>取消</button>
      </span>
    ) : (
      <>
        <button title="只采集这一家企业" disabled={busy || rowAction === `crawl:${company.name}`} onClick={() => onRun(company.name)}>{rowAction === `crawl:${company.name}` ? <LoaderCircle className="spin" size={13} /> : <Zap size={13} />}定向采集</button>
        <button title="只采集这一家的上海住建信用分" disabled={rowAction === `score:${company.name}`} onClick={() => onCreditScore?.(company.name)}>{rowAction === `score:${company.name}` ? <LoaderCircle className="spin" size={13} /> : <Gauge size={13} />}信用分</button>
        <button className="row-remove" title="从固定名单移除" onClick={() => onConfirmRemove?.(company.name)}><Trash2 size={13} /></button>
      </>
    )}
  </td>}<td><ChevronRight size={16} /></td></tr>)}</tbody></table></div>;
}

function formatCreditScore(score: number | null | undefined) {
  if (score == null || Number.isNaN(Number(score))) return "--";
  return Number(score).toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}

function RecordSection({ title, items, kind }: { title: string; items: Record<string, unknown>[]; kind: "permission" | "penalty" }) {
  const value = (item: Record<string, unknown>, ...keys: string[]) => keys.map((key) => String(item[key] ?? "").trim()).find(Boolean) || "--";
  return <section className={`company-record-section ${kind}`}><header><strong>{title}</strong><span>{items.length} 条</span></header>{items.length ? <div>{items.slice(0, 6).map((item, index) => <article key={index}><strong>{kind === "permission" ? value(item, "许可项目名称", "行政许可决定文书名称") : value(item, "处罚名称", "行政处罚决定文书名称")}</strong><p>{kind === "permission" ? value(item, "许可机关", "许可决定机关") : value(item, "处罚结果", "处罚内容")}</p><small>{kind === "permission" ? value(item, "许可决定日期", "数据更新时间") : value(item, "处罚决定日期", "数据更新时间")}</small></article>)}</div> : <p className="record-none">当前没有{title}记录</p>}</section>;
}

function EvidenceCaptureList({
  captures,
  onOpen,
  onPackage,
}: {
  captures: EvidenceCapture[];
  onOpen: (capture: EvidenceCapture, asset: "overview" | "panel" | "item", item?: EvidenceItem) => void;
  onPackage: (capture: EvidenceCapture) => void;
}) {
  return <section className="company-evidence-section">
    <header><div><Images size={17} /><span><strong>官网证据图</strong><small>每次采集独立留存，包含零处罚企业</small></span></div><b>{captures.length} 个批次</b></header>
    {captures.length ? <div className="company-evidence-list">{captures.map((capture) => <article key={capture.id}>
      <div className="evidence-capture-head"><span><strong>{new Date(capture.capturedAt).toLocaleString("zh-CN", { hour12: false })}</strong><small>官网处罚 {capture.penaltyCount} 条 · 批次 #{capture.id}</small></span><button onClick={() => void onPackage(capture)}><Download size={13} />证据包</button></div>
      <div className="evidence-capture-actions">
        {capture.hasOverview && <button onClick={() => onOpen(capture, "overview")}><Eye size={13} />整页截图</button>}
        {capture.hasPanel && <button onClick={() => onOpen(capture, "panel")}><Images size={13} />处罚栏目</button>}
        {capture.hasHtml && <a href={crawlerAssetUrl(`/monitor/evidence/${capture.id}/assets/html`)} target="_blank" rel="noreferrer"><FileCode2 size={13} />页面源码</a>}
        {capture.hasMetadata && <a href={crawlerAssetUrl(`/monitor/evidence/${capture.id}/assets/metadata`)} target="_blank" rel="noreferrer"><FileCheck2 size={13} />证据清单</a>}
      </div>
      {!!capture.items.length && <div className="evidence-item-actions">{capture.items.map((item) => item.hasImage && <button key={item.id} onClick={() => onOpen(capture, "item", item)}><CircleAlert size={12} />{item.documentNumber}</button>)}</div>}
    </article>)}</div> : <div className="company-evidence-empty"><Images size={20} /><span><strong>尚无可调用证据</strong><small>下一次成功采集后，整页截图会出现在这里。</small></span></div>}
  </section>;
}
