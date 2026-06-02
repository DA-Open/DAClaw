import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectOption } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useI18n } from "@/i18n";

type FieldMeta = { label?: string; description?: string };

const FIELD_ZH: Record<string, FieldMeta> = {
  model: { label: "默认模型", description: "默认使用的模型，例如 anthropic/claude-sonnet-4.6。" },
  model_context_length: { label: "模型上下文长度", description: "上下文窗口覆盖值，0 表示根据模型元数据自动检测。" },
  fallback_providers: { label: "备用提供商", description: "主模型不可用时可尝试的备用提供商。" },
  toolsets: { label: "工具集", description: "启用的工具集，多个值用英文逗号分隔。" },
  file_read_max_chars: { label: "文件读取最大字符数", description: "单次读取文件时允许载入的最大字符数。" },
  prefill_messages_file: { label: "预填消息文件", description: "会话启动时预先加载的消息文件路径。" },
  timezone: { label: "时区", description: "运行时使用的时区，例如 Asia/Shanghai。" },
  command_allowlist: { label: "命令白名单", description: "允许自动执行的命令前缀。" },

  "agent.max_turns": { label: "最大轮次", description: "单次会话允许的最大模型/工具循环次数。" },
  "agent.gateway_timeout": { label: "网关超时", description: "网关等待代理回复的最长时间，单位为秒。" },
  "agent.gateway_timeout_warning": { label: "网关超时警告", description: "接近超时时触发警告的时间。" },
  "agent.gateway_notify_interval": { label: "网关通知间隔", description: "后台任务状态通知的间隔。" },
  "agent.restart_drain_timeout": { label: "重启排空超时", description: "重启前等待当前任务收尾的时间，单位为秒。" },
  "agent.service_tier": { label: "服务等级", description: "模型服务等级设置，空值表示使用默认等级。" },
  "agent.tool_use_enforcement": { label: "工具使用策略", description: "控制代理是否必须使用工具或按默认策略决定。" },
  "context.engine": { label: "上下文引擎", description: "上下文管理使用的引擎。" },
  "network.force_ipv4": { label: "强制 IPv4", description: "网络请求是否强制使用 IPv4。" },
  "cron.wrap_response": { label: "包装定时任务回复", description: "定时任务输出是否进行额外包装。" },
  "checkpoints.enabled": { label: "启用检查点", description: "是否保存任务检查点。" },
  "checkpoints.max_snapshots": { label: "最大快照数", description: "每个任务最多保存的检查点快照数量。" },
  "skills.external_dirs": { label: "外部技能目录", description: "额外扫描的技能目录列表。" },
  "smart_model_routing.enabled": { label: "启用智能模型路由", description: "是否根据任务复杂度自动选择模型。" },
  "smart_model_routing.max_simple_chars": { label: "简单任务最大字符数", description: "用于判断简单任务的字符数阈值。" },
  "smart_model_routing.max_simple_words": { label: "简单任务最大词数", description: "用于判断简单任务的词数阈值。" },

  "display.compact": { label: "紧凑显示", description: "减少界面留白，让终端/面板显示更紧凑。" },
  "display.personality": { label: "界面风格", description: "CLI/助手的显示风格名称。配置值本身使用英文标识。" },
  "display.resume_display": { label: "恢复会话显示", description: "恢复会话时如何展示历史消息。" },
  "display.busy_input_mode": { label: "运行中输入模式", description: "代理运行时用户继续输入内容的处理方式。" },
  "display.bell_on_complete": { label: "完成时响铃", description: "任务完成后是否播放提示音。" },
  "display.show_reasoning": { label: "显示推理", description: "是否在界面中显示模型推理摘要。" },
  "display.streaming": { label: "流式输出", description: "是否边生成边显示回复。" },
  "display.inline_diffs": { label: "内联差异", description: "是否以内联方式展示文件差异。" },
  "display.interim_assistant_messages": { label: "中间助手消息", description: "是否显示执行过程中的临时助手消息。" },
  "display.show_cost": { label: "显示成本", description: "是否显示本次会话的成本估算。" },
  "display.tool_preview_length": { label: "工具预览长度", description: "工具结果预览的最大长度。" },
  "display.tool_progress_command": { label: "工具进度命令", description: "是否显示工具进度相关命令。" },
  "display.skin": { label: "CLI 皮肤", description: "命令行界面的视觉皮肤。" },
  "human_delay.mode": { label: "模拟输入延迟", description: "用于模拟打字延迟的模式。" },
  "human_delay.min_ms": { label: "最小延迟毫秒", description: "模拟输入延迟的最小毫秒数。" },
  "human_delay.max_ms": { label: "最大延迟毫秒", description: "模拟输入延迟的最大毫秒数。" },

  "terminal.backend": { label: "终端后端", description: "命令执行使用的终端环境。" },
  "terminal.cwd": { label: "终端工作目录", description: "终端命令默认执行目录。" },
  "terminal.timeout": { label: "终端超时", description: "终端命令默认超时时间。" },
  "terminal.persistent_shell": { label: "持久 Shell", description: "是否复用同一个 shell 会话。" },
  "terminal.container_cpu": { label: "容器 CPU", description: "容器后端可用的 CPU 配额。" },
  "terminal.container_memory": { label: "容器内存", description: "容器后端可用的内存配额。" },
  "terminal.container_disk": { label: "容器磁盘", description: "容器后端可用的磁盘配额。" },
  "terminal.container_persistent": { label: "持久容器", description: "容器环境是否持久保存。" },
  "terminal.docker_image": { label: "Docker 镜像", description: "Docker 后端使用的镜像。" },
  "terminal.docker_volumes": { label: "Docker 卷", description: "挂载到 Docker 容器的卷列表。" },
  "terminal.docker_forward_env": { label: "Docker 转发环境变量", description: "传入 Docker 容器的环境变量列表。" },
  "terminal.docker_mount_cwd_to_workspace": { label: "挂载当前目录到工作区", description: "是否把当前目录挂载到容器工作区。" },
  "terminal.env_passthrough": { label: "透传环境变量", description: "允许透传给终端环境的变量列表。" },
  "terminal.modal_image": { label: "Modal 镜像", description: "Modal 后端使用的镜像。" },
  "terminal.modal_mode": { label: "Modal 模式", description: "Modal 后端的运行模式。" },
  "terminal.daytona_image": { label: "Daytona 镜像", description: "Daytona 后端使用的镜像。" },
  "terminal.singularity_image": { label: "Singularity 镜像", description: "Singularity 后端使用的镜像。" },

  "approvals.mode": { label: "审批模式", description: "危险命令执行前的审批策略。" },
  "approvals.timeout": { label: "审批超时", description: "等待审批的最长时间。" },
  "security.redact_secrets": { label: "密钥脱敏", description: "日志和输出中是否隐藏敏感密钥。" },
  "security.tirith_enabled": { label: "启用 Tirith 安全检查", description: "是否启用 Tirith 安全策略检查。" },
  "security.tirith_fail_open": { label: "Tirith 失败放行", description: "安全检查失败时是否继续执行。" },
  "security.tirith_path": { label: "Tirith 路径", description: "Tirith 可执行文件路径。" },
  "security.tirith_timeout": { label: "Tirith 超时", description: "Tirith 安全检查超时时间。" },
  "privacy.redact_pii": { label: "个人信息脱敏", description: "是否自动脱敏个人身份信息。" },
  "security.website_blocklist.enabled": { label: "启用网站阻止列表", description: "是否启用网站访问阻止列表。" },
  "security.website_blocklist.domains": { label: "阻止域名", description: "禁止访问的域名列表。" },
  "security.website_blocklist.shared_files": { label: "共享阻止列表文件", description: "额外加载的网站阻止列表文件。" },

  "compression.enabled": { label: "启用压缩", description: "是否启用上下文压缩。" },
  "compression.threshold": { label: "压缩阈值", description: "触发上下文压缩的阈值。" },
  "compression.target_ratio": { label: "目标压缩比例", description: "上下文压缩后的目标比例。" },
  "compression.protect_last_n": { label: "保护最近消息数", description: "压缩时保留最近 N 条消息。" },
  "memory.memory_enabled": { label: "启用记忆", description: "是否启用长期记忆。" },
  "memory.provider": { label: "记忆提供方", description: "长期记忆使用的提供方。" },
  "memory.memory_char_limit": { label: "记忆字符上限", description: "单条记忆允许的最大字符数。" },
  "memory.user_profile_enabled": { label: "启用用户画像", description: "是否启用用户画像记忆。" },
  "memory.user_char_limit": { label: "用户画像字符上限", description: "用户画像允许的最大字符数。" },

  "browser.allow_private_urls": { label: "允许私有网址", description: "浏览器工具是否允许访问私有/本地地址。" },
  "browser.command_timeout": { label: "浏览器命令超时", description: "浏览器自动化命令的超时时间。" },
  "browser.inactivity_timeout": { label: "浏览器空闲超时", description: "浏览器会话空闲多久后关闭。" },
  "browser.record_sessions": { label: "记录浏览器会话", description: "是否保存浏览器会话记录。" },
  "browser.camofox.managed_persistence": { label: "Camofox 持久化管理", description: "是否由 DAClaw 管理 Camofox 的持久化数据。" },

  "logging.level": { label: "日志级别", description: "agent.log 的日志级别。" },
  "logging.max_size_mb": { label: "日志最大大小 MB", description: "单个日志文件的最大大小。" },
  "logging.backup_count": { label: "日志备份数量", description: "保留的日志备份文件数量。" },

  "voice.auto_tts": { label: "自动语音播报", description: "是否自动把回复转为语音。" },
  "voice.record_key": { label: "录音快捷键", description: "触发录音的快捷键。" },
  "voice.max_recording_seconds": { label: "最大录音秒数", description: "单次录音允许的最长时间。" },
  "voice.silence_threshold": { label: "静音阈值", description: "判断静音的音量阈值。" },
  "voice.silence_duration": { label: "静音持续时间", description: "判定录音结束所需的静音时长。" },
  "stt.enabled": { label: "启用语音转文字", description: "是否启用语音识别输入。" },
  "stt.provider": { label: "语音转文字提供方", description: "语音识别使用的提供方。" },
  "tts.provider": { label: "文字转语音提供方", description: "语音播报使用的提供方。" },

  provider: { label: "模型提供商" },
  api_key: { label: "API 密钥" },
  base_url: { label: "基础 URL" },
  timeout: { label: "超时" },
  enabled: { label: "启用" },
  host: { label: "主机" },
  port: { label: "端口" },
  token: { label: "令牌" },
};

const TOKEN_ZH: Record<string, string> = {
  agent: "代理",
  analysis: "分析",
  api: "API",
  approval: "审批",
  approvals: "审批",
  audit: "审计",
  auto: "自动",
  backend: "后端",
  backup: "备份",
  base: "基础",
  bell: "提示音",
  blocklist: "阻止列表",
  busy: "运行中",
  browser: "浏览器",
  cache: "缓存",
  camofox: "Camofox",
  channels: "频道",
  chars: "字符数",
  checkpoints: "检查点",
  command: "命令",
  compact: "紧凑显示",
  complete: "完成",
  compression: "压缩",
  config: "配置",
  container: "容器",
  context: "上下文",
  cost: "成本",
  count: "数量",
  cpu: "CPU",
  cron: "定时任务",
  cwd: "工作目录",
  daytona: "Daytona",
  default: "默认",
  delegation: "委托",
  diffs: "差异",
  dirs: "目录",
  discovery: "发现",
  disk: "磁盘",
  display: "显示",
  docker: "Docker",
  domains: "域名",
  download: "下载",
  drain: "排空",
  edge: "Edge",
  enabled: "启用",
  enforcement: "策略",
  env: "环境变量",
  external: "外部",
  fail: "失败",
  fallback: "备用",
  file: "文件",
  filter: "过滤器",
  fixed: "固定",
  flush: "刷新",
  force: "强制",
  forward: "转发",
  free: "自由",
  gateway: "网关",
  guardrail: "护栏",
  high: "高",
  history: "历史",
  human: "模拟人工",
  identifier: "标识符",
  image: "镜像",
  inline: "内联",
  input: "输入",
  interim: "中间",
  iterations: "迭代次数",
  key: "密钥",
  language: "语言",
  level: "级别",
  local: "本地",
  logging: "日志",
  low: "低",
  managed: "托管",
  max: "最大",
  mb: "MB",
  mcp: "MCP",
  medium: "中",
  memory: "记忆",
  mention: "提及",
  messages: "消息",
  min: "最小",
  mistral: "Mistral",
  modal: "Modal",
  mode: "模式",
  model: "模型",
  ms: "毫秒",
  network: "网络",
  openai: "OpenAI",
  path: "路径",
  persistence: "持久化",
  persistent: "持久",
  preview: "预览",
  private: "私有",
  progress: "进度",
  protect: "保护",
  provider: "提供方",
  providers: "提供方",
  quiet: "静默",
  ratio: "比例",
  read: "读取",
  reactions: "表情反馈",
  record: "记录",
  redact: "脱敏",
  reasoning: "推理",
  refresh: "刷新",
  region: "区域",
  require: "需要",
  response: "回复",
  resume: "恢复会话",
  restart: "重启",
  save: "保存",
  search: "搜索",
  secrets: "密钥",
  security: "安全",
  service: "服务",
  session: "会话",
  sessions: "会话",
  shared: "共享",
  shell: "Shell",
  show: "显示",
  silence: "静音",
  simple: "简单",
  singularity: "Singularity",
  skin: "皮肤",
  skill: "技能",
  skills: "技能",
  smart: "智能",
  snapshot: "快照",
  snapshots: "快照",
  speech: "语音",
  stream: "流",
  streaming: "流式输出",
  stt: "语音转文字",
  target: "目标",
  terminal: "终端",
  text: "文本",
  tier: "等级",
  timeout: "超时",
  timezone: "时区",
  tool: "工具",
  toolsets: "工具集",
  trajectories: "轨迹",
  tts: "文字转语音",
  turns: "轮次",
  url: "URL",
  urls: "网址",
  use: "使用",
  user: "用户",
  validation: "校验",
  version: "版本",
  vision: "视觉",
  voice: "语音",
  warning: "警告",
  web: "网页",
  website: "网站",
  window: "窗口",
  words: "词数",
  wrap: "包装",
};

const OPTION_ZH: Record<string, Record<string, string>> = {
  "display.resume_display": { minimal: "精简（minimal）", full: "完整（full）", off: "关闭（off）" },
  "display.busy_input_mode": { queue: "排队（queue）", interrupt: "打断当前任务（interrupt）", block: "禁止输入（block）" },
  "display.skin": { default: "默认（default）", ares: "Ares 皮肤（ares）", mono: "黑白简洁（mono）", slate: "蓝灰开发风（slate）" },
  "human_delay.mode": { off: "关闭（off）", typing: "模拟打字（typing）", fixed: "固定延迟（fixed）" },
  "agent.service_tier": { "": "默认", auto: "自动（auto）", default: "默认（default）", flex: "弹性（flex）" },
  "approvals.mode": { ask: "询问确认（ask）", yolo: "自动允许（yolo）", deny: "自动拒绝（deny）" },
  "context.engine": { default: "默认（default）", custom: "自定义（custom）" },
  "delegation.reasoning_effort": { "": "默认", low: "低（low）", medium: "中（medium）", high: "高（high）" },
  "terminal.backend": { local: "本地（local）", docker: "Docker", ssh: "SSH", modal: "Modal", daytona: "Daytona", singularity: "Singularity" },
  "terminal.modal_mode": { sandbox: "沙箱（sandbox）", function: "函数（function）" },
  "memory.provider": { builtin: "内置（builtin）", honcho: "Honcho" },
  "stt.provider": { local: "本地（local）", openai: "OpenAI", mistral: "Mistral" },
  "tts.provider": { edge: "Edge", elevenlabs: "ElevenLabs", openai: "OpenAI", neutts: "NeuTTS" },
  "logging.level": { DEBUG: "调试（DEBUG）", INFO: "信息（INFO）", WARNING: "警告（WARNING）", ERROR: "错误（ERROR）" },
};

function humanizeKey(schemaKey: string): string {
  const rawLabel = schemaKey.split(".").pop() ?? schemaKey;
  return rawLabel.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function zhMeta(schemaKey: string) {
  const tail = schemaKey.split(".").pop() ?? schemaKey;
  return FIELD_ZH[schemaKey] ?? FIELD_ZH[tail];
}

function zhLabelFromKey(schemaKey: string): string {
  const tail = schemaKey.split(".").pop() ?? schemaKey;
  const parts = tail.split("_").filter((part) => part !== "on");
  const translated = parts.map((part) => TOKEN_ZH[part] ?? part).join("");
  return translated || humanizeKey(schemaKey);
}

function zhDescription(schemaKey: string): string {
  const meta = zhMeta(schemaKey);
  if (meta?.description) return meta.description;
  const parts = schemaKey.split(".").map((part) => FIELD_ZH[part]?.label ?? TOKEN_ZH[part] ?? zhLabelFromKey(part));
  return parts.length > 1 ? parts.join(" → ") : `${zhLabelFromKey(schemaKey)}设置项。`;
}

function fieldLabel(schemaKey: string, locale: string): string {
  return locale === "zh" ? zhMeta(schemaKey)?.label ?? zhLabelFromKey(schemaKey) : humanizeKey(schemaKey);
}

function optionLabel(schemaKey: string, option: string, locale: string): string {
  if (locale !== "zh") return option || "(none)";
  return OPTION_ZH[schemaKey]?.[option] ?? (option || "默认");
}

function FieldHint({
  schema,
  schemaKey,
  locale,
}: {
  schema: Record<string, unknown>;
  schemaKey: string;
  locale: string;
}) {
  const keyPath = schemaKey.includes(".") ? schemaKey : "";
  const description = locale === "zh" ? zhDescription(schemaKey) : schema.description ? String(schema.description) : "";

  if (!keyPath && !description) return null;

  return (
    <div className="flex flex-col gap-0.5">
      {keyPath && <span className="text-[10px] font-mono text-muted-foreground/50">{keyPath}</span>}
      {description && <span className="text-xs text-muted-foreground/70">{description}</span>}
    </div>
  );
}

export function AutoField({
  schemaKey,
  schema,
  value,
  onChange,
}: AutoFieldProps) {
  const { locale } = useI18n();
  const label = fieldLabel(schemaKey, locale);

  if (schema.type === "boolean") {
    return (
      <div className="flex items-center justify-between gap-4">
        <div className="flex flex-col gap-0.5">
          <Label className="text-sm">{label}</Label>
          <FieldHint schema={schema} schemaKey={schemaKey} locale={locale} />
        </div>
        <Switch checked={!!value} onCheckedChange={onChange} />
      </div>
    );
  }

  if (schema.type === "select") {
    const options = (schema.options as string[]) ?? [];
    return (
      <div className="grid gap-1.5">
        <Label className="text-sm">{label}</Label>
        <FieldHint schema={schema} schemaKey={schemaKey} locale={locale} />
        <Select value={String(value ?? "")} onValueChange={(v) => onChange(v)}>
          {options.map((opt) => (
            <SelectOption key={opt} value={opt}>
              {optionLabel(schemaKey, opt, locale)}
            </SelectOption>
          ))}
        </Select>
      </div>
    );
  }

  if (schema.type === "number") {
    return (
      <div className="grid gap-1.5">
        <Label className="text-sm">{label}</Label>
        <FieldHint schema={schema} schemaKey={schemaKey} locale={locale} />
        <Input
          type="number"
          value={value === undefined || value === null ? "" : String(value)}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") {
              onChange(0);
              return;
            }
            const n = Number(raw);
            if (!Number.isNaN(n)) onChange(n);
          }}
        />
      </div>
    );
  }

  if (schema.type === "text") {
    return (
      <div className="grid gap-1.5">
        <Label className="text-sm">{label}</Label>
        <FieldHint schema={schema} schemaKey={schemaKey} locale={locale} />
        <textarea
          className="flex min-h-[80px] w-full border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
        />
      </div>
    );
  }

  if (schema.type === "list") {
    return (
      <div className="grid gap-1.5">
        <Label className="text-sm">{label}</Label>
        <FieldHint schema={schema} schemaKey={schemaKey} locale={locale} />
        <Input
          value={Array.isArray(value) ? value.join(", ") : String(value ?? "")}
          onChange={(e) =>
            onChange(
              e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            )
          }
          placeholder={locale === "zh" ? "用英文逗号分隔" : "comma-separated values"}
        />
      </div>
    );
  }

  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const obj = value as Record<string, unknown>;
    return (
      <div className="grid gap-3 border border-border p-3">
        <Label className="text-xs font-medium">{label}</Label>
        <FieldHint schema={schema} schemaKey={schemaKey} locale={locale} />
        {Object.entries(obj).map(([subKey, subVal]) => (
          <div key={subKey} className="grid gap-1">
            <Label className="text-xs text-muted-foreground">{locale === "zh" ? zhLabelFromKey(subKey) : subKey}</Label>
            <Input
              value={String(subVal ?? "")}
              onChange={(e) => onChange({ ...obj, [subKey]: e.target.value })}
              className="text-xs"
            />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="grid gap-1.5">
      <Label className="text-sm">{label}</Label>
      <FieldHint schema={schema} schemaKey={schemaKey} locale={locale} />
      <Input value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

interface AutoFieldProps {
  schemaKey: string;
  schema: Record<string, unknown>;
  value: unknown;
  onChange: (v: unknown) => void;
}
