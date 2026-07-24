"use client";

/* 设置中心：API 凭据、存储位置与固定名单管理。 */

import {
  Building2,
  CheckCircle2,
  Database,
  Eye,
  EyeOff,
  FileText,
  FolderOpen,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { crawlerApiJson } from "./crawler-api";

type EnvField = {
  key: string;
  label: string;
  hint: string;
  sensitive: boolean;
  value: string;
  configured: boolean;
};

type PathField = {
  key: string;
  label: string;
  hint: string;
  value: string;
};

type ActivePaths = {
  outputDir: string;
  statePath: string;
  companyFile: string;
};

type SettingsViewProps = {
  companies: string[];
  onCompaniesChanged: () => Promise<void> | void;
  onToast: (message: string) => void;
};

const PATH_ICONS = [FolderOpen, Database, FileText];

export default function SettingsView({ companies, onCompaniesChanged, onToast }: SettingsViewProps) {
  const [envFields, setEnvFields] = useState<EnvField[]>([]);
  const [envDraft, setEnvDraft] = useState<Record<string, string>>({});
  const [envLoading, setEnvLoading] = useState(true);
  const [savingEnv, setSavingEnv] = useState(false);
  const [visibleKeys, setVisibleKeys] = useState<Record<string, boolean>>({});

  const [pathFields, setPathFields] = useState<PathField[]>([]);
  const [pathDraft, setPathDraft] = useState<Record<string, string>>({});
  const [activePaths, setActivePaths] = useState<ActivePaths | null>(null);
  const [savingPaths, setSavingPaths] = useState(false);
  const [pathsDirty, setPathsDirty] = useState(false);

  const [removing, setRemoving] = useState("");
  const [confirmRemove, setConfirmRemove] = useState("");

  const loadEnv = useCallback(async () => {
    setEnvLoading(true);
    try {
      const payload = await crawlerApiJson<{ fields: EnvField[] }>("/settings/env");
      setEnvFields(payload.fields);
      const draft: Record<string, string> = {};
      for (const field of payload.fields) draft[field.key] = field.sensitive ? "" : field.value;
      setEnvDraft(draft);
    } catch (caught) {
      onToast(caught instanceof Error ? caught.message : "设置读取失败");
    } finally {
      setEnvLoading(false);
    }
  }, [onToast]);

  const loadPaths = useCallback(async () => {
    try {
      const payload = await crawlerApiJson<{ fields: PathField[]; active: ActivePaths }>("/settings/paths");
      setPathFields(payload.fields);
      setActivePaths(payload.active);
      const draft: Record<string, string> = {};
      for (const field of payload.fields) draft[field.key] = field.value;
      setPathDraft(draft);
      setPathsDirty(false);
    } catch (caught) {
      onToast(caught instanceof Error ? caught.message : "存储位置读取失败");
    }
  }, [onToast]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadEnv();
      void loadPaths();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadEnv, loadPaths]);

  const saveEnv = async () => {
    setSavingEnv(true);
    try {
      const result = await crawlerApiJson<{ saved: string[]; count: number }>("/settings/env", {
        method: "PUT",
        body: JSON.stringify({ values: envDraft }),
      });
      onToast(result.count ? `已保存 ${result.count} 项凭据配置` : "没有需要保存的修改");
      await loadEnv();
    } catch (caught) {
      onToast(caught instanceof Error ? caught.message : "保存失败");
    } finally {
      setSavingEnv(false);
    }
  };

  const savePaths = async () => {
    const changed: Record<string, string> = {};
    for (const field of pathFields) {
      const next = (pathDraft[field.key] || "").trim();
      if (next && next !== field.value) changed[field.key] = next;
    }
    if (!Object.keys(changed).length) {
      onToast("存储位置没有修改");
      return;
    }
    setSavingPaths(true);
    try {
      await crawlerApiJson("/settings/paths", {
        method: "PUT",
        body: JSON.stringify({ values: changed }),
      });
      onToast("存储位置已保存，重启服务后完全生效");
      await loadPaths();
    } catch (caught) {
      onToast(caught instanceof Error ? caught.message : "保存失败");
    } finally {
      setSavingPaths(false);
    }
  };

  const removeCompany = async (name: string) => {
    setRemoving(name);
    try {
      const result = await crawlerApiJson<{ count: number }>(
        `/monitor/companies/${encodeURIComponent(name)}`,
        { method: "DELETE" },
      );
      onToast(`已从固定名单移除「${name}」，当前 ${result.count} 家`);
      setConfirmRemove("");
      await onCompaniesChanged();
    } catch (caught) {
      onToast(caught instanceof Error ? caught.message : "移除失败");
    } finally {
      setRemoving("");
    }
  };

  return (
    <div className="settings-page">
      <article className="monitor-panel settings-panel">
        <header className="settings-panel-head">
          <span className="settings-panel-icon keys"><KeyRound size={18} /></span>
          <div>
            <h2>API 凭据与采集参数</h2>
            <p>保存后立即写入 .env.local 并对新任务生效；敏感项留空表示保持不变。</p>
          </div>
          <button className="settings-save" disabled={savingEnv || envLoading} onClick={() => void saveEnv()}>
            {savingEnv ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
            保存凭据
          </button>
        </header>
        {envLoading ? (
          <div className="settings-loading"><LoaderCircle className="spin" size={20} />正在读取配置…</div>
        ) : (
          <div className="settings-grid">
            {envFields.map((field) => {
              const visible = !!visibleKeys[field.key];
              return (
                <label key={field.key} className="settings-field">
                  <span className="settings-field-label">
                    {field.label}
                    <em className={field.configured ? "" : "unset"}>{field.configured ? "已配置" : "未配置"}</em>
                  </span>
                  <span className="settings-input-wrap">
                    <input
                      type={field.sensitive && !visible ? "password" : "text"}
                      value={envDraft[field.key] ?? ""}
                      placeholder={field.sensitive ? (field.configured ? "已保存（留空保持不变）" : "未设置") : ""}
                      onChange={(event) => setEnvDraft((draft) => ({ ...draft, [field.key]: event.target.value }))}
                      autoComplete="off"
                      spellCheck={false}
                    />
                    {field.sensitive && (
                      <button
                        type="button"
                        className="settings-eye"
                        onClick={() => setVisibleKeys((map) => ({ ...map, [field.key]: !visible }))}
                        aria-label={visible ? "隐藏" : "显示"}
                      >
                        {visible ? <EyeOff size={14} /> : <Eye size={14} />}
                      </button>
                    )}
                  </span>
                  <small>{field.hint}</small>
                </label>
              );
            })}
          </div>
        )}
      </article>

      <article className="monitor-panel settings-panel">
        <header className="settings-panel-head">
          <span className="settings-panel-icon paths"><FolderOpen size={18} /></span>
          <div>
            <h2>存储位置</h2>
            <p>修改结果目录、数据库与名单文件位置。保存后需重启服务（start_project.py）才会切换。</p>
          </div>
          <button className="settings-save" disabled={savingPaths} onClick={() => void savePaths()}>
            {savingPaths ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
            保存位置
          </button>
        </header>
        {pathsDirty && (
          <div className="settings-restart-note">
            <RefreshCw size={14} />存储位置已修改并写入 .env.local，重启服务后新位置才会生效。
          </div>
        )}
        <div className="settings-grid paths">
          {pathFields.map((field, index) => {
            const Icon = PATH_ICONS[index % PATH_ICONS.length];
            return (
              <label key={field.key} className="settings-field">
                <span className="settings-field-label"><Icon size={14} />{field.label}</span>
                <span className="settings-input-wrap">
                  <input
                    type="text"
                    value={pathDraft[field.key] ?? ""}
                    onChange={(event) => setPathDraft((draft) => ({ ...draft, [field.key]: event.target.value }))}
                    spellCheck={false}
                  />
                </span>
                <small>{field.hint}</small>
              </label>
            );
          })}
        </div>
        {activePaths && (
          <div className="settings-active-paths">
            <strong>当前生效位置</strong>
            <code>结果目录：{activePaths.outputDir}</code>
            <code>数据库：{activePaths.statePath}</code>
            <code>名单文件：{activePaths.companyFile}</code>
          </div>
        )}
      </article>

      <article className="monitor-panel settings-panel">
        <header className="settings-panel-head">
          <span className="settings-panel-icon companies"><Building2 size={18} /></span>
          <div>
            <h2>固定企业名单 · {companies.length} 家</h2>
            <p>在这里移除不再监控的企业；新增企业请使用顶栏「添加企业」。移除只影响监控名单，不会删除已采集的历史数据。</p>
          </div>
        </header>
        {companies.length ? (
          <ul className="settings-company-list">
            {companies.map((name) => (
              <li key={name}>
                <span className="settings-company-avatar">{name.charAt(0)}</span>
                <strong>{name}</strong>
                {confirmRemove === name ? (
                  <span className="settings-confirm">
                    确认移除？
                    <button
                      className="confirm-yes"
                      disabled={removing === name}
                      onClick={() => void removeCompany(name)}
                    >
                      {removing === name ? <LoaderCircle className="spin" size={13} /> : <CheckCircle2 size={13} />}
                      确认
                    </button>
                    <button className="confirm-no" onClick={() => setConfirmRemove("")}>取消</button>
                  </span>
                ) : (
                  <button className="settings-remove" onClick={() => setConfirmRemove(name)}>
                    <Trash2 size={14} />移除
                  </button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <div className="settings-loading"><ShieldCheck size={18} />名单为空，请先在顶栏添加企业。</div>
        )}
      </article>
    </div>
  );
}
