import { useEffect, useRef, useState } from "react";
import { X, Database, Loader2, Plug, FileUp } from "lucide-react";
import { api } from "@/lib/api";
import type { DADbType, DAConnectResponse } from "@/lib/api";

// Modal for connecting a live external database (Postgres/MySQL/Snowflake/…).
// Connecting a real DB is the primary path; dropping a file is just one basic
// alternative — so this dialog is the prominent entry point.

const FIELD_LABEL: Record<string, string> = {
  host: "Host", port: "Port", user: "User", password: "Password",
  database: "Database", schema: "Schema",
};
const FIELD_PLACEHOLDER: Record<string, string> = {
  host: "localhost", port: "", user: "", password: "", database: "", schema: "",
};

export function ConnectDbDialog({
  open,
  onClose,
  onConnected,
}: {
  open: boolean;
  onClose: () => void;
  onConnected: (r: DAConnectResponse) => void;
}) {
  const [types, setTypes] = useState<DADbType[]>([]);
  const [dbtype, setDbtype] = useState("postgres");
  const [mode, setMode] = useState<"fields" | "dsn">("fields");
  const [dsn, setDsn] = useState("");
  const [fields, setFields] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pickedFile, setPickedFile] = useState<string | null>(null); // server path after upload
  const [pickedName, setPickedName] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    api.daDbTypes().then((r) => {
      setTypes(r.types);
      if (r.types.length && !r.types.find((t) => t.id === dbtype)) setDbtype(r.types[0].id);
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;
  const current = types.find((t) => t.id === dbtype);
  const activeFields = current?.fields ?? ["host", "port", "user", "password", "database"];
  // file-based DBs open the native file picker + upload, instead of a path field
  const isFileType = dbtype === "sqlite" || dbtype === "duckdb";

  const pickFile = () => fileRef.current?.click();
  const onFileChosen = async (fl: FileList | null) => {
    if (!fl?.length) return;
    setBusy(true);
    setError(null);
    try {
      const up = await api.uploadChatFile(fl[0]);
      setPickedFile(up.path);
      setPickedName(up.name);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      let params: Record<string, unknown>;
      if (isFileType) {
        if (!pickedFile) { setError("Choose a file first."); setBusy(false); return; }
        params = { dbtype, database: pickedFile, name: pickedName?.replace(/\.[^.]+$/, "") };
      } else if (mode === "dsn") {
        params = { dbtype, dsn };
      } else {
        params = { dbtype, ...fields, port: fields.port ? Number(fields.port) : undefined };
      }
      const r = await api.daConnectDb(params);
      if (r.ok) { onConnected(r); onClose(); }
      else setError(r.error || "connect failed");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-background/60 backdrop-blur-sm" onClick={onClose}>
      <div className="w-[440px] max-w-[92vw] rounded-2xl border border-border bg-card shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <Plug className="h-4 w-4 text-[#2DD4BF]" />
          <span className="text-sm font-semibold">Connect a database</span>
          <button onClick={onClose} className="ml-auto rounded p-1 text-muted-foreground hover:bg-secondary"><X className="h-4 w-4" /></button>
        </div>
        <div className="space-y-3 p-4">
          {/* type selector */}
          <div className="flex flex-wrap gap-1.5">
            {types.map((t) => (
              <button
                key={t.id}
                onClick={() => setDbtype(t.id)}
                className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${
                  dbtype === t.id ? "border-[#2DD4BF] bg-[#2DD4BF]/10 text-[#2DD4BF]" : "border-border text-muted-foreground hover:bg-secondary"
                }`}
              >
                <Database className="h-3 w-3" /> {t.label}
              </button>
            ))}
          </div>

          {/* hidden native file picker for sqlite/duckdb */}
          <input
            ref={fileRef}
            type="file"
            accept=".duckdb,.ddb,.sqlite,.sqlite3,.db,.db3"
            className="hidden"
            onChange={(e) => { onFileChosen(e.target.files); e.target.value = ""; }}
          />

          {isFileType ? (
            <div className="space-y-2">
              <button
                onClick={pickFile}
                disabled={busy}
                className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-border px-3 py-3 text-sm text-muted-foreground hover:border-[#2DD4BF] hover:text-foreground disabled:opacity-50"
              >
                <FileUp className="h-4 w-4" /> {pickedName ? "Choose a different file…" : "Choose a file…"}
              </button>
              {pickedName && (
                <div className="flex items-center gap-2 rounded-md border border-border bg-secondary/40 px-2.5 py-1.5 text-xs">
                  <Database className="h-3.5 w-3.5 text-[#2DD4BF]" />
                  <span className="min-w-0 flex-1 truncate">{pickedName}</span>
                </div>
              )}
              <div className="text-[11px] text-muted-foreground">
                Pick a {dbtype === "duckdb" ? ".duckdb" : ".sqlite / .db"} file from your computer.
              </div>
            </div>
          ) : (
            <>
              {/* mode toggle */}
              <div className="flex gap-1 text-xs">
                <button onClick={() => setMode("fields")} className={`rounded px-2 py-1 ${mode === "fields" ? "bg-secondary font-medium" : "text-muted-foreground"}`}>Fields</button>
                <button onClick={() => setMode("dsn")} className={`rounded px-2 py-1 ${mode === "dsn" ? "bg-secondary font-medium" : "text-muted-foreground"}`}>Connection string</button>
              </div>

              {mode === "dsn" ? (
                <input
                  value={dsn}
                  onChange={(e) => setDsn(e.target.value)}
                  placeholder="postgresql://user:pass@host:5432/dbname"
                  className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-sm font-mono-ui"
                />
              ) : (
                <div className="grid grid-cols-2 gap-2">
                  {activeFields.map((f) => (
                    <div key={f} className={f === "database" || f === "host" ? "col-span-2" : ""}>
                      <label className="mb-0.5 block text-[11px] text-muted-foreground">{FIELD_LABEL[f] ?? f}</label>
                      <input
                        type={f === "password" ? "password" : "text"}
                        value={fields[f] ?? (f === "port" && current?.default_port ? String(current.default_port) : "")}
                        onChange={(e) => setFields((s) => ({ ...s, [f]: e.target.value }))}
                        placeholder={FIELD_PLACEHOLDER[f] ?? ""}
                        className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm"
                      />
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
          {current?.hint && <div className="text-[11px] text-muted-foreground">{current.hint}</div>}
          {error && <div className="rounded-md border border-red-200 bg-red-50 px-2.5 py-1.5 text-xs text-red-600">{error}</div>}
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
          <button onClick={onClose} className="rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:bg-secondary">Cancel</button>
          <button onClick={connect} disabled={busy} className="inline-flex items-center gap-1.5 rounded-lg bg-[#2DD4BF] px-4 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plug className="h-4 w-4" />} Connect
          </button>
        </div>
      </div>
    </div>
  );
}
