// ── Lightweight i18n for the Reel web app (Chinese / English) ────────────
// Strategy:
//   * On page load (and on every DOM mutation) walk added nodes, replace
//     known English text with the active dictionary's translation.
//   * Translate placeholder / title / aria-label / alt attributes too.
//   * Provide a global `tr(s)` for explicit JS-side calls.
//   * Persist the choice in localStorage; default = Chinese.
//   * A floating switcher in the top-right toggles between zh / en
//     (reload on switch — simplest and most reliable).
//
// The dictionary is keyed by the EXACT English source string. Keep this
// file the single source of truth for translations across login.html,
// projects.html, and workflow.html. Strings not in the dictionary are
// passed through unchanged, and a small pattern table covers dynamic
// "N nodes" / "N projects" / etc.

(function () {
  const STORAGE_KEY = 'app.lang';
  const DEFAULT_LANG = 'zh';

  // ── Translations ──────────────────────────────────────────────────────
  const ZH = {
    // ── Login page ────────────────────────────────────────────────────
    'Direct your whole film on a single canvas':
      '在一块画布上指挥整部影片',
    'Compose prompts, references, and shots as nodes — then run any branch, any subset, or the whole graph at once':
      '将提示词、参考素材和镜头编排为节点 — 随时运行任意分支、子集或整张图',
    'v 4.2 · spring release': 'v 4.2 · 春季版本',
    '2,400 studios · 18M renders': '2,400 家工作室 · 1,800 万次渲染',
    'Email': '邮箱',
    'Password': '密码',
    'Forgot?': '忘记密码？',
    'Keep me signed in on this device': '在本设备保持登录',
    'Sign in': '登录',
    'Show password': '显示密码',
    'Hide password': '隐藏密码',
    "Reel is invite-only. No public sign-up yet — request access and we'll get back within 48 hours":
      'Reel 仅限受邀使用,暂未开放注册 — 申请试用后我们将在 48 小时内回复',
    'Enter your email and password to continue': '请输入邮箱和密码以继续',
    'Signing in…': '登录中…',
    'Sign-in failed. Please try again': '登录失败,请重试',
    'Network error — please check your connection and retry':
      '网络异常 — 请检查网络后重试',
    'Terms': '条款',
    'Privacy': '隐私',
    'Status': '状态',
    'Noir Detective · Scene 01': 'Noir Detective · 第 01 场',
    'Detective · turnaround': '侦探 · 多面图',
    'Shot 02 · wide': '镜头 02 · 远景',
    'Coffee ritual': '咖啡仪式',
    'Live': '直播中',

    // ── Projects page ─────────────────────────────────────────────────
    'Search projects…': '搜索项目…',
    'New project': '新建项目',
    'Account': '账号',
    'Sign out': '退出登录',
    'All projects': '全部项目',
    'Recent': '最近',
    'Favorites': '收藏',
    'Archived': '已归档',
    'Your projects': '我的项目',
    'All': '全部',
    'Grid view': '网格视图',
    'List view': '列表视图',
    'Name': '名称',
    'Updated': '更新时间',
    'No projects yet': '暂无项目',
    'Create your first project to start building video generation workflows':
      '创建第一个项目,开始搭建视频生成工作流',
    'Create first project': '创建第一个项目',
    'Start a new project': '开始一个新项目',
    'Start blank or from a template': '从空白开始',
    'Give your project a name to get started.': '为项目起一个名称即可开始。',
    'Archive': '归档',
    'Duplicate': '复制',
    'Delete': '删除',
    'Clear': '清除',
    'Start blank or pick a template to scaffold the graph for you':
      '从空白开始,或选择模板自动搭建节点图',
    'Rename project': '重命名项目',
    'Project name': '项目名称',
    'Untitled project': '未命名项目',
    'Template': '模板',
    'Cancel': '取消',
    'Create project': '创建项目',
    'Save': '保存',
    'Share': '分享',
    'Share…': '分享…',
    'Share this project': '分享该项目',
    'Add a collaborator': '添加协作者',
    'Members': '成员',
    // Share-modal permission labels — kept verbose ("Can view" / "Can edit")
    // so they don't collide with the Edit Library card ("Edit" → "剪辑").
    'Can view': '浏览',
    'Can edit': '编辑',
    'View': '浏览',
    'Read': '浏览',
    'Read & write': '编辑',
    'Add': '添加',
    'Close': '关闭',
    'Email is required': '请填写邮箱',
    'Loading…': '加载中…',
    'No collaborators yet — invite someone above.': '暂无协作者 — 请在上方邀请。',
    'owner': '所有者',
    'view': '浏览',
    'edit': '编辑',
    'shared · view': '已分享 · 浏览',
    'shared · edit': '已分享 · 编辑',
    'Delete this project?': '确定删除该项目?',
    'This permanently removes the canvas, generated outputs, and run history. This cannot be undone':
      '将永久删除画布、生成内容和运行记录,此操作不可撤销',
    'Open': '打开',
    'Rename': '重命名',
    'Toggle favorite': '切换收藏',
    'Restore': '还原',
    'Blank canvas': '空白画布',
    'Start with an empty graph': '从空图开始',
    'Image → Video': '图像 → 视频',
    'Still image into a 5s clip': '静态图像生成 5 秒片段',
    'Script → Shot list': '剧本 → 分镜',
    'Break a scene into shots': '将场景拆解为镜头',
    'B-roll montage': '空镜剪辑',
    'Stitch clips with VO': '用旁白拼接片段',
    'Character turnaround': '角色多面图',
    'Reference → 4 angles': '参考图 → 4 个视角',
    '30-second ad': '30 秒广告',
    'Hook · body · payoff': '钩子 · 主体 · 收尾',
    'Failed to load projects': '项目加载失败',
    'Duplicated': '已复制',
    'Duplicate failed': '复制失败',
    'Restored': '已还原',
    'Project deleted': '项目已删除',
    'Delete failed': '删除失败',
    'Just created': '刚刚创建',
    'Ready': '就绪',

    // ── Workflow page ─────────────────────────────────────────────────
    'Workflow': '工作流',
    'Run selected': '运行所选',
    'Run all': '全部运行',
    'Toggle left panel': '切换左侧面板',
    'Toggle right panel': '切换右侧面板',
    'Library': '节点库',
    'Input': '输入',
    'Output': '输出',
    'Add nodes': '添加节点',
    'Text': '文本',
    'Image': '图像',
    'Video': '视频',
    'Audio': '音频',
    'Upload': '上传',
    'Edit': '剪辑',
    'Multi-modal → script (Doubao)': '多模态 → 脚本(豆包)',
    'Generate or transform an image': '生成或转换图像',
    'Generate or animate a video': '生成或动画化视频',
    'Generate music': '生成音乐',
    'AI-planned trim · speed · concat': 'AI 自动剪辑 · 调速 · 拼接',
    'Choose file': '选择文件',
    'Run': '运行',
    'Play': '播放',
    'Click to add as Upload node': '点击作为上传节点添加',
    'Remove': '移除',
    'Reveal on canvas': '在画布中定位',
    'Re-run': '重新运行',
    'key': '键',
    'value': '值',

    // Library card descriptions (kept for older designs)
    'Image · video · audio source': '图像 · 视频 · 音频来源',

    // Toasts / inline status
    'Uploading…': '上传中…',
    'Upload done': '上传完成',
    'Upload failed': '上传失败',
    'Use the upload button on the node to choose a file':
      '请通过节点上的上传按钮选择文件',
    'This node type provides data — no generation needed':
      '该节点类型用于提供数据 — 无需生成',
    'Already running': '正在运行',
    'Cycle detected — cannot run': '检测到循环 — 无法运行',
    'Nothing runnable selected': '未选择可运行的节点',
    'Edge selected — press Delete to remove': '已选择连线 — 按 Delete 键删除',
    'Cannot connect these nodes': '这些节点之间无法连接',
    'Select nodes first (click or drag a box)': '请先选择节点(点击或框选)',
    'Canvas is empty': '画布为空',
    'Copied': '已复制',
    'Copy failed': '复制失败',

    // Inputs / outputs / variables
    'Project inputs': '项目输入',
    'Project variables': '项目变量',
    'Add input': '添加输入',
    'image · video · audio · text · subtitles': '图像 · 视频 · 音频 · 文本 · 字幕',
    '+ Add variable': '+ 添加变量',
    '+ Add file': '+ 添加文件',
    'Choose file…': '选择文件…',
    'Choose a file': '选择文件',
    'No project inputs yet.': '暂无项目输入。',
    'Drop files above to share them across nodes.':
      '将文件拖到上方,在多个节点之间共享。',
    'No files yet.': '暂无文件。',
    'Run nodes to generate outputs,': '运行节点以生成输出,',
    'or add an Upload node as a source.': '或添加上传节点作为来源。',
    'Generated': '已生成',
    'SOURCE': '来源',
    'LATEST': '最新',
    '↓ download': '↓ 下载',
    '↗ open': '↗ 打开',

    // Right panel / inspector
    'Generated text': '生成的文本',
    'Generated audio': '生成的音频',
    'Copy': '复制',
    'NAME': '名称',
    'PROMPT': '提示词',
    'INSTRUCTION': '指令',
    'Mode': '模式',
    'Auto': '自动',
    'Duration': '时长',
    'Duration (s)': '时长 (秒)',
    'Aspect ratio': '宽高比',
    'Resolution': '分辨率',
    'Model': '模型',
    'Run this node': '运行此节点',
    'Connected inputs': '已连接的输入',
    'Generated audio file': '生成的音频文件',
    'Voiceover or sfx': '旁白或音效',
    'Reference asset': '参考素材',

    // Helper / placeholder text
    'Write a 30s product script highlighting the selling points above…':
      '写一段 30 秒的产品脚本,突出上面的卖点…',
    'A misty mountain at dawn…': '黎明中云雾缭绕的群山…',
    'Camera slowly pushes in, leaves rustle in the breeze…':
      '镜头缓缓推进,树叶在微风中沙沙作响…',
    'A fox sitting in a snowy forest, cinematic 4K…':
      '一只狐狸坐在雪林中,电影感 4K…',
    'Describe the scene; the reference image guides style/subject…':
      '描述场景;参考图像将引导风格与主体…',
    'The model receives all upstream inputs (text · images · video refs) plus this instruction.':
      '模型会接收上游所有输入(文本 · 图像 · 视频参考)以及该指令。',
    'I2V mode — describes motion applied to the upstream image (optional).':
      'I2V 模式 — 描述施加在上游图像上的运动(可选)。',
    'T2V mode — connect an Image node to switch to I2V, or pick MM2V to combine image + prompt.':
      'T2V 模式 — 连接图像节点可切换到 I2V,或选择 MM2V 组合图像与提示词。',
    'Multimodal (全能参考) — uses the reference image plus this prompt to compose a brand-new scene.':
      '多模态(全能参考)— 使用参考图像与该提示词合成全新场景。',
    'Upbeat cinematic background music, orchestral with piano and strings…':
      '欢快的电影感背景音乐,管弦加钢琴与弦乐…',
    'Describe the audio. MiniMax TTS generates speech or music based on your description. Set MINIMAX_API_KEY and optionally MINIMAX_VOICE_ID on the server.':
      '描述需要的音频。MiniMax TTS 将根据描述生成语音或音乐。请在服务端设置 MINIMAX_API_KEY,可选 MINIMAX_VOICE_ID。',
    "Trim clip 1 to 3s · 2x speed on clip 2 · fade-in 0.5s · cinematic cool look · caption 'Chapter One' for first 3s · crossfade 0.5s between clips · watermark bottom-right…":
      "片段 1 裁到 3 秒 · 片段 2 加速 2 倍 · 0.5 秒淡入 · 电影冷调 · 前 3 秒加字幕 'Chapter One' · 片段间 0.5 秒交叉淡入 · 右下角加水印…",
    'AI converts instructions into an ffmpeg edit plan. Supports: trim · speed · mute · transitions (fade, wipe, slide, etc.) · color/look · per-clip captions/titles · subtitle burn-in (.srt upload) · watermark (image upload) · picture-in-picture · title card · background audio · fade in/out.':
      'AI 会将指令转换为 ffmpeg 剪辑方案。支持:裁剪 · 调速 · 静音 · 转场(淡入淡出、擦除、滑动等) · 调色 · 单片段字幕/标题 · 字幕烧录(.srt 上传) · 水印(图像上传) · 画中画 · 标题卡 · 背景音乐 · 淡入淡出。',
    'Reference anywhere as': '在任意位置使用',
    'resolved before the prompt hits any API. Keys and values may use Chinese / any Unicode (e.g.':
      '会在提示词送达接口前完成替换。键和值均支持中文 / 任意 Unicode(例如',
    '_credits': '积分',
    '— credits': '— 积分',

    // Status labels
    'Idle': '空闲',
    'Queued': '排队中',
    'Running': '运行中',
    'Generating…': '生成中…',
    'Editing…': '剪辑中…',
    'Queued…': '排队中…',
    'Error': '错误',

    // Buttons
    'Replace': '替换',
    '+ Upload': '+ 上传',

    // Empty / placeholder strings
    'Type an instruction or connect inputs': '输入指令或连接输入',
    'Type a prompt or upload a file': '输入提示词或上传文件',
    'No output yet': '暂无输出',
    'Connect Video or Upload nodes to edit': '连接视频或上传节点以进行剪辑',
    'Drop or click to upload': '拖入或点击上传',
    'image · video · audio': '图像 · 视频 · 音频',

    // Footer / copyright
    '© 2026 Reel Studio': '© 2026 Reel Studio',

    // Misc
    'Saved': '已保存',
    'Saving…': '保存中…',
    'Offline': '离线',
    'see Files': '查看输出',
    'Files': '文件',
  };

  // Dynamic / count patterns. Each entry is [regex, replacement-template].
  // Matches against the trimmed string only.
  const PATTERNS_ZH = [
    [/^(\d+)\s+selected$/, '已选 $1 项'],
    [/^(\d+)\s+nodes?$/i, '$1 个节点'],
    [/^(\d+)\s+projects?$/i, '$1 个项目'],
    [/^(\d+)\s+credits?$/i, '$1 积分'],
    [/^(\d+)\s+runs?$/i, '$1 次运行'],
    [/^(\d+)\s+run\(s\)$/i, '$1 次运行'],
    [/^(\d+)\s+clips?$/i, '$1 个片段'],
    [/^(\d+)\s+edges?$/i, '$1 条连线'],
    [/^(\d+)\s+edges?\s+deleted$/i, '已删除 $1 条连线'],
    [/^(\d+)\s+nodes?\s+deleted$/i, '已删除 $1 个节点'],
    [/^(\d+)\s+projects?\s+deleted$/i, '已删除 $1 个项目'],
    [/^queued\s+#(\d+)$/i, '排队中 #$1'],
    [/^Run\s+(\d+)$/, '运行 $1'],
    [/^(\d+)\s+run\s+·\s+see Files$/i, '$1 次运行 · 查看输出'],
    [/^(\d+)\s+runs\s+·\s+see Files$/i, '$1 次运行 · 查看输出'],
    [/^(\d+)\s+run\s+·\s+see Output$/i, '$1 次运行 · 查看输出'],
    [/^(\d+)\s+runs\s+·\s+see Output$/i, '$1 次运行 · 查看输出'],
    [/^Failed to load projects:\s*(.+)$/, '项目加载失败: $1'],
    [/^Duplicate failed:\s*(.+)$/, '复制失败: $1'],
    [/^Delete failed:\s*(.+)$/, '删除失败: $1'],
    [/^Renamed to\s+"(.+)"$/, '已重命名为 "$1"'],
    [/^Shared with\s+(\S+)\s*\((view|edit|read|read & write)\)$/i, (_m, who, perm) => {
      const map = { view: '浏览', read: '浏览', edit: '编辑', 'read & write': '编辑' };
      return '已与 ' + who + ' 分享(' + (map[perm.toLowerCase()] || perm) + ')';
    }],
    [/^Removed\s+(\S+)$/, '已移除 $1'],
    [/^(\S+)\s+→\s+(view|edit|read|read & write)$/i, (_m, who, perm) => {
      const map = { view: '浏览', read: '浏览', edit: '编辑', 'read & write': '编辑' };
      return who + ' → ' + (map[perm.toLowerCase()] || perm);
    }],
    [/^Remove failed:\s*(.+)$/, '移除失败: $1'],
    [/^Added\s+(\S+)$/, '已添加 $1'],
    [/^(\w+)\s+done$/i, (_m, k) => {
      const map = { text: '文本', image: '图像', video: '视频', audio: '音频', edit: '剪辑', upload: '上传' };
      return (map[k.toLowerCase()] || k) + '完成';
    }],
    [/^(\w+):\s*(.+)$/, (m, kind, msg) => {
      const map = { text: '文本', image: '图像', video: '视频', audio: '音频', edit: '剪辑', upload: '上传' };
      const k = map[kind.toLowerCase()];
      return k ? (k + ': ' + msg) : m;
    }],
  ];

  // ── Engine ────────────────────────────────────────────────────────────
  const dicts = { en: {}, zh: ZH };
  const SUPPORTED = new Set(Object.keys(dicts));
  let lang = (function () {
    try { return localStorage.getItem(STORAGE_KEY) || DEFAULT_LANG; }
    catch { return DEFAULT_LANG; }
  })();
  if (!SUPPORTED.has(lang)) lang = DEFAULT_LANG;

  // Don't translate text inside these tags
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'CODE', 'PRE']);
  // Don't translate text inside elements with this attribute (user content)
  const NO_I18N = 'data-no-i18n';

  function tr(s) {
    if (!s || typeof s !== 'string') return s;
    if (lang === 'en') return s;
    const dict = dicts[lang];
    if (!dict) return s;
    // Preserve surrounding whitespace
    const m = s.match(/^(\s*)([\s\S]*?)(\s*)$/);
    const lead = m[1], body = m[2], trail = m[3];
    if (!body) return s;
    if (dict[body]) return lead + dict[body] + trail;
    for (const [re, repl] of PATTERNS_ZH) {
      if (re.test(body)) {
        return lead + body.replace(re, repl) + trail;
      }
    }
    return s;
  }

  function translateAttr(el, name) {
    const v = el.getAttribute(name);
    if (!v) return;
    const t = tr(v);
    if (t !== v) el.setAttribute(name, t);
  }

  function translateNode(node) {
    if (!node) return;
    if (node.nodeType === 3) {  // TEXT
      const parent = node.parentElement;
      if (parent && (SKIP_TAGS.has(parent.tagName) || parent.closest('[' + NO_I18N + ']'))) return;
      const v = node.textContent;
      const t = tr(v);
      if (t !== v) node.textContent = t;
      return;
    }
    if (node.nodeType !== 1) return;  // not an element
    if (SKIP_TAGS.has(node.tagName)) return;
    if (node.hasAttribute && node.hasAttribute(NO_I18N)) return;
    ['placeholder', 'title', 'aria-label', 'alt', 'data-tooltip'].forEach(a => translateAttr(node, a));
    for (const child of node.childNodes) translateNode(child);
  }

  function applyAll() {
    if (lang === 'en') return;
    translateNode(document.body);
  }

  let mo = null;
  function startObserver() {
    if (mo || lang === 'en') return;
    mo = new MutationObserver(muts => {
      for (const m of muts) {
        if (m.type === 'characterData') translateNode(m.target);
        else if (m.type === 'attributes') {
          translateAttr(m.target, m.attributeName);
        } else {
          for (const n of m.addedNodes) translateNode(n);
        }
      }
    });
    mo.observe(document.documentElement, {
      childList: true, subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ['placeholder', 'title', 'aria-label', 'alt', 'data-tooltip'],
    });
  }

  function setLang(newLang) {
    if (!SUPPORTED.has(newLang) || newLang === lang) return;
    try { localStorage.setItem(STORAGE_KEY, newLang); } catch {}
    location.reload();
  }

  function makeSwitcher() {
    if (document.getElementById('lang-switcher')) return;
    const btn = document.createElement('button');
    btn.id = 'lang-switcher';
    btn.type = 'button';
    btn.setAttribute(NO_I18N, '');
    btn.textContent = lang === 'zh' ? 'EN' : '中';
    btn.title = lang === 'zh' ? 'Switch to English' : '切换为中文';
    btn.addEventListener('click', () => setLang(lang === 'zh' ? 'en' : 'zh'));

    // Prefer inline placement next to the user-menu (workflow + projects
    // topbars). Fall back to a floating top-right button (login page).
    const userMenu = document.querySelector('.user-menu');
    if (userMenu && userMenu.parentElement) {
      btn.className = 'lang-switcher-inline';
      btn.style.cssText = [
        'flex-shrink:0', 'min-width:34px', 'height:30px', 'padding:0 10px',
        'margin-right:6px',
        'font:600 11px/1 system-ui,-apple-system,Segoe UI,Roboto,sans-serif',
        'color:var(--fg-1, rgba(255,255,255,0.85))',
        'background:var(--bg-2, rgba(255,255,255,0.06))',
        'border:1px solid var(--line, rgba(255,255,255,0.12))',
        'border-radius:6px', 'cursor:pointer',
        'letter-spacing:0.05em',
      ].join(';');
      userMenu.parentElement.insertBefore(btn, userMenu);
    } else {
      btn.style.cssText = [
        'position:fixed', 'top:14px', 'right:18px', 'z-index:99999',
        'min-width:34px', 'height:28px', 'padding:0 10px',
        'font:600 11px/1 system-ui,-apple-system,Segoe UI,Roboto,sans-serif',
        'color:rgba(255,255,255,0.92)',
        'background:rgba(20,20,20,0.55)',
        'border:1px solid rgba(255,255,255,0.18)',
        'border-radius:6px', 'cursor:pointer',
        'backdrop-filter:blur(10px)',
        '-webkit-backdrop-filter:blur(10px)',
        'letter-spacing:0.05em',
      ].join(';');
      document.body.appendChild(btn);
    }
  }


  // ── Theme switcher (dark / light) ──────────────────────────────────────────
  const THEME_KEY = 'app.theme';

  function getTheme() {
    try { return localStorage.getItem(THEME_KEY) || 'dark'; } catch { return 'dark'; }
  }

  function applyTheme(theme) {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
  }

  function setTheme(theme) {
    try { localStorage.setItem(THEME_KEY, theme); } catch {}
    applyTheme(theme);
    updateThemeButton();
  }

  function toggleTheme() {
    setTheme(getTheme() === 'dark' ? 'light' : 'dark');
  }

  function updateThemeButton() {
    const btn = document.getElementById('theme-switcher');
    if (!btn) return;
    const isDark = getTheme() === 'dark';
    btn.textContent = isDark ? '☀' : '🌙';
    btn.title = isDark ? (lang === 'zh' ? '切换浅色模式' : 'Switch to light mode') : (lang === 'zh' ? '切换深色模式' : 'Switch to dark mode');
  }

  function makeThemeSwitcher() {
    if (document.getElementById('theme-switcher')) return;
    const btn = document.createElement('button');
    btn.id = 'theme-switcher';
    btn.type = 'button';
    btn.setAttribute(NO_I18N, '');
    btn.addEventListener('click', toggleTheme);

    const userMenu = document.querySelector('.user-menu');
    if (userMenu && userMenu.parentElement) {
      btn.className = 'lang-switcher-inline';
      btn.style.cssText = [
        'flex-shrink:0', 'width:30px', 'height:30px', 'padding:0',
        'margin-right:6px',
        'font-size:14px', 'line-height:1',
        'color:var(--fg-1, rgba(255,255,255,0.85))',
        'background:var(--bg-2, rgba(255,255,255,0.06))',
        'border:1px solid var(--line, rgba(255,255,255,0.12))',
        'border-radius:6px', 'cursor:pointer',
        'display:grid', 'place-items:center',
      ].join(';');
      userMenu.parentElement.insertBefore(btn, userMenu);
    } else {
      btn.style.cssText = [
        'position:fixed', 'top:14px', 'right:56px', 'z-index:99999',
        'width:28px', 'height:28px', 'padding:0',
        'font-size:13px', 'line-height:1',
        'color:rgba(255,255,255,0.92)',
        'background:rgba(20,20,20,0.55)',
        'border:1px solid rgba(255,255,255,0.18)',
        'border-radius:6px', 'cursor:pointer',
        'backdrop-filter:blur(10px)',
        '-webkit-backdrop-filter:blur(10px)',
        'display:grid', 'place-items:center',
      ].join(';');
      document.body.appendChild(btn);
    }
    updateThemeButton();
  }

  // Apply saved theme ASAP (before DOM ready to avoid flash)
  applyTheme(getTheme());

  function init() {
    applyAll();
    makeSwitcher();
    makeThemeSwitcher();
    startObserver();
  }


  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }

  // Public API
  window.tr = tr;
  window.appLang = () => lang;
  window.setAppLang = setLang;
})();
