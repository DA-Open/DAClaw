export type Locale = "en" | "zh";

export interface Translations {
  // ── Common ──
  common: {
    save: string;
    saving: string;
    cancel: string;
    close: string;
    delete: string;
    refresh: string;
    retry: string;
    search: string;
    loading: string;
    create: string;
    creating: string;
    set: string;
    replace: string;
    clear: string;
    live: string;
    off: string;
    enabled: string;
    disabled: string;
    active: string;
    inactive: string;
    unknown: string;
    untitled: string;
    none: string;
    form: string;
    noResults: string;
    of: string;
    page: string;
    msgs: string;
    tools: string;
    match: string;
    other: string;
    configured: string;
    removed: string;
    failedToToggle: string;
    failedToRemove: string;
    failedToReveal: string;
    collapse: string;
    expand: string;
    general: string;
    messaging: string;
  };

  // ── App shell ──
  app: {
    brand: string;
    brandShort: string;
    webUi: string;
    footer: {
      name: string;
      org: string;
    };
    nav: {
      status: string;
      chat: string;
      sessions: string;
      analytics: string;
      logs: string;
      cron: string;
      skills: string;
      config: string;
      keys: string;
    };
  };

  // ── Status page ──
  status: {
    agent: string;
    gateway: string;
    activeSessions: string;
    recentSessions: string;
    connectedPlatforms: string;
    running: string;
    starting: string;
    failed: string;
    stopped: string;
    connected: string;
    disconnected: string;
    error: string;
    notRunning: string;
    startFailed: string;
    pid: string;
    runningRemote: string;
    noneRunning: string;
    gatewayFailedToStart: string;
    lastUpdate: string;
    platformError: string;
    platformDisconnected: string;
  };

  // ── Sessions page ──
  sessions: {
    title: string;
    searchPlaceholder: string;
    noSessions: string;
    noMatch: string;
    startConversation: string;
    noMessages: string;
    untitledSession: string;
    deleteSession: string;
    previousPage: string;
    nextPage: string;
    roles: {
      user: string;
      assistant: string;
      system: string;
      tool: string;
    };
  };

  // ── Analytics page ──
  analytics: {
    period: string;
    totalTokens: string;
    totalSessions: string;
    apiCalls: string;
    dailyTokenUsage: string;
    dailyBreakdown: string;
    perModelBreakdown: string;
    input: string;
    output: string;
    total: string;
    noUsageData: string;
    startSession: string;
    date: string;
    model: string;
    tokens: string;
    perDayAvg: string;
    acrossModels: string;
    inOut: string;
  };

  // ── Logs page ──
  logs: {
    title: string;
    autoRefresh: string;
    file: string;
    level: string;
    component: string;
    lines: string;
    noLogLines: string;
  };

  // ── Cron page ──
  cron: {
    newJob: string;
    nameOptional: string;
    namePlaceholder: string;
    prompt: string;
    promptPlaceholder: string;
    schedule: string;
    schedulePlaceholder: string;
    deliverTo: string;
    scheduledJobs: string;
    noJobs: string;
    last: string;
    next: string;
    pause: string;
    resume: string;
    triggerNow: string;
    delivery: {
      local: string;
      telegram: string;
      discord: string;
      slack: string;
      email: string;
    };
  };

  // ── Skills page ──
  skills: {
    title: string;
    searchPlaceholder: string;
    enabledOf: string;
    all: string;
    noSkills: string;
    noSkillsMatch: string;
    skillCount: string;
    resultCount: string;
    noDescription: string;
    toolsets: string;
    toolsetLabel: string;
    noToolsetsMatch: string;
    setupNeeded: string;
    disabledForCli: string;
    more: string;
  };

  // ── Config page ──
  config: {
    configPath: string;
    exportConfig: string;
    importConfig: string;
    resetDefaults: string;
    rawYaml: string;
    searchResults: string;
    fields: string;
    noFieldsMatch: string;
    configSaved: string;
    yamlConfigSaved: string;
    failedToSave: string;
    failedToSaveYaml: string;
    failedToLoadRaw: string;
    configImported: string;
    invalidJson: string;
    categories: {
      general: string;
      agent: string;
      terminal: string;
      display: string;
      delegation: string;
      memory: string;
      compression: string;
      security: string;
      browser: string;
      voice: string;
      tts: string;
      stt: string;
      logging: string;
      discord: string;
      auxiliary: string;
    };
  };

  // ── Env / Keys page ──
  env: {
    description: string;
    changesNote: string;
    hideAdvanced: string;
    showAdvanced: string;
    llmProviders: string;
    providersConfigured: string;
    getKey: string;
    notConfigured: string;
    notSet: string;
    keysCount: string;
    enterValue: string;
    replaceCurrentValue: string;
    showValue: string;
    hideValue: string;
  };

  // ── OAuth ──
  oauth: {
    title: string;
    providerLogins: string;
    description: string;
    connected: string;
    expired: string;
    notConnected: string;
    runInTerminal: string;
    noProviders: string;
    login: string;
    disconnect: string;
    managedExternally: string;
    copied: string;
    cli: string;
    copyCliCommand: string;
    connect: string;
    sessionExpires: string;
    initiatingLogin: string;
    exchangingCode: string;
    connectedClosing: string;
    loginFailed: string;
    sessionExpired: string;
    reOpenAuth: string;
    reOpenVerification: string;
    submitCode: string;
    pasteCode: string;
    waitingAuth: string;
    enterCodePrompt: string;
    pkceStep1: string;
    pkceStep2: string;
    pkceStep3: string;
    flowLabels: {
      pkce: string;
      device_code: string;
      external: string;
    };
    expiresIn: string;
  };

  // ── Language switcher ──
  language: {
    switchTo: string;
  };

  // ── Data Agent + ETL (Data Engineering) UI ──
  da: {
    title: string;
    subtitleAsk: string;
    subtitleTable: string;
    conversations: string;
    selectForChat: string;
    askingAbout: string;
    clearSelection: string;
    columns: string;
    collapseCols: string;
    newChat: string;
    rename: string;
    deleteChat: string;
    dataSources: string;
    sourcePlaceholder: string;
    add: string;
    dragToConnectHint: string;
    emptyTitle: string;
    emptyBody: string;
    thinking: string;
    askPlaceholder: string;
    send: string;
    clearInput: string;
    stop: string;
    copy: string;
    copied: string;
    artifacts: string;
    loadingSample: string;
    noSample: string;
    download: string;
    downloadXlsx: string;
    xlsxPreviewNote: string;
    rows: string;
    previewFirst: string;
    renderingPreview: string;
    rendering: string;
    artifactsInChat: string;
    traceCall: string;
    traceAnalyze: string;
    traceRunning: string;
    traceSteps: string;
    inProgress: string;
    tStarting: string;
    tRunning: string;
    tWriting: string;
    tAnalyzing: string;
    tPlanning: string;
    tGenChart: string;
    tAnalyzeTable: string;
    tChartDone: string;
    tAnalyzeDone: string;
    tRun: string;
    tDone: string;
    tProblem: string;
    tFailed: string;
    stCall: string;
    stDone: string;
    stTool: string;
    stError: string;
    stStatus: string;
    ranThisSql: string;
    sqlLabel: string;
    etlTitle: string;
    connectDb: string;
    etlHistory: string;
    analyzeInDA: string;
    analyzeInDAHint: string;
    connectedOk: string;
    etlConnectedTables: string;
    etlSubtitle: string;
    etlNoSource: string;
    etlGoalPlaceholder: string;
    etlBuild: string;
    etlBuilding: string;
    etlBuildingHint: string;
    etlEmptyTitle: string;
    etlEmptyBody: string;
    etlFailed: string;
    etlModels: string;
    etlModelsBuilt: string;
    etlExported: string;
    etlFiles: string;
    etlPreview: string;
    layerRaw: string;
    layerStaging: string;
    layerIntermediate: string;
    layerMarts: string;
    etlAskPlaceholder: string;
    etlAsk: string;
    etlAnalyzeHere: string;
    etlOpenInDA: string;
    etlRematerializing: string;
    missionControl: string;
    missionOnline: string;
    missionTables: string;
    missionKnowledge: string;
    missionScope: string;
    missionKicker: string;
    missionHero: string;
    missionBody: string;
    missionCompile: string;
    missionCompileBody: string;
    missionValidate: string;
    missionValidateBody: string;
    missionSynthesize: string;
    missionSynthesizeBody: string;
    knowledgeTitle: string;
    knowledgeEmpty: string;
    compiledMission: string;
    validated: string;
    layer: string;
    parallel: string;
    pipelineStudio: string;
    playbooks: string;
    playbookEmpty: string;
    skillsTitle: string;
    skillsSearch: string;
    skillsEmpty: string;
    skillNew: string;
    skillOn: string;
    skillOff: string;
    skillEnable: string;
    skillDisable: string;
    skillSavedTitle: string;
    skillKept: string;
    skillRejected: string;
    skillQuality: string;
    clarifyTitle: string;
    clarifyOther: string;
    clarifySend: string;
    nodeInspector: string;
    nodeInputs: string;
    nodeSql: string;
    nodeOutput: string;
    nodeNotRunYet: string;
    nodeNoOutput: string;
    nodeCompleted: string;
    nodeRunning: string;
    nodeError: string;
    nodeSkipped: string;
    nodePlanned: string;
    approvePlan: string;
    approveHint: string;
    editPlan: string;
    compilePlan: string;
    launchMission: string;
    dismissPlan: string;
    missionReady: string;
    removeStep: string;
  };
  lb: {
    title: string;
    subtitle: string;
    refresh: string;
    loading: string;
    failed: string;
    harnesses: string;
    benchmarks: string;
    models: string;
    coverage: string;
    podium: string;
    tabRanking: string;
    tabMatrix: string;
    tabCombos: string;
    rankingNote: string;
    overallScore: string;
    harnessRanking: string;
    benchmarkMatrix: string;
    bestCombos: string;
    evaluate: string;
    evaluateTitle: string;
    evaluateDesc: string;
    running: string;
    tasks: string;
    sotaCeiling: string;
    vsCeiling: string;
    harnessCol: string;
    tracked: string;
    acrossLevels: string;
    whatIsHarness: string;
    tabModels: string;
    modelLeaderboard: string;
    intelligenceIndex: string;
    agenticScore: string;
    creator: string;
    license: string;
    open: string;
    proprietary: string;
    all: string;
    openWeights: string;
    filterAll: string;
    creators: string;
    modelCol: string;
    rank: string;
    modelNote: string;
  };
}
