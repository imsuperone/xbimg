// ==========================================================================
// 消息转图助手 · Android 16 (Material 3 Expressive) Web Client
// Version: 1.0.1
// ==========================================================================
(function () {
  "use strict";

  const PLUGIN_ID = "astrbot_plugin_xbimg";

  // ---- 安全 AstrBot 通信桥接层 ----
  function getBridge() {
    try {
      if (window.AstrBotPluginPage && typeof window.AstrBotPluginPage.apiGet === "function") {
        return window.AstrBotPluginPage;
      }
      if (window.bridge && typeof window.bridge.apiGet === "function") {
        return window.bridge;
      }
      if (window.parent && window.parent.AstrBotPluginPage && typeof window.parent.AstrBotPluginPage.apiGet === "function") {
        return window.parent.AstrBotPluginPage;
      }
      if (window.parent && window.parent.bridge && typeof window.parent.bridge.apiGet === "function") {
        return window.parent.bridge;
      }
    } catch (e) {}
    return null;
  }

  // 通知父级或 AstrBot 容器本页面已就绪
  function notifyReady() {
    try {
      const b = getBridge();
      if (b && typeof b.ready === "function") {
        b.ready();
      }
    } catch (e) {}

    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ type: "PLUGIN_PAGE_READY", plugin: PLUGIN_ID }, "*");
        window.parent.postMessage({ type: "PAGE_READY", plugin: PLUGIN_ID }, "*");
      }
    } catch (e) {}
  }

  let _detectedPrefix = null;

  async function tryFetchJson(url, options = {}) {
    const res = await fetch(url, options);
    if (!res.ok) {
      const errText = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}: ${errText || res.statusText}`);
    }
    return await res.json();
  }

  const api = {
    async get(endpoint, params = {}) {
      const b = getBridge();
      if (b && typeof b.apiGet === "function") {
        try {
          return await b.apiGet(endpoint, params);
        } catch (e) {
          console.warn(`[msg2img] bridge.apiGet(${endpoint}) 失败，回退 fetch:`, e);
        }
      }
      const qs = new URLSearchParams(params).toString();
      const queryStr = qs ? `?${qs}` : "";

      if (_detectedPrefix) {
        try {
          return await tryFetchJson(`${_detectedPrefix}${endpoint}${queryStr}`);
        } catch (e) {}
      }

      const prefixes = [
        `/${PLUGIN_ID}/`,
        `/astrbot_plugin_msg2img/`,
        `/api/plugins/${PLUGIN_ID}/`,
        `/api/plugins/astrbot_plugin_msg2img/`,
        `api/`,
        `./api/`,
        `./`,
      ];
      for (const p of prefixes) {
        try {
          const res = await tryFetchJson(`${p}${endpoint}${queryStr}`);
          _detectedPrefix = p;
          return res;
        } catch (e) {}
      }
      throw new Error(`无法连接至插件后端 API (${endpoint})`);
    },

    async post(endpoint, data = {}) {
      const b = getBridge();
      if (b && typeof b.apiPost === "function") {
        try {
          return await b.apiPost(endpoint, data);
        } catch (e) {
          console.warn(`[xbimg] bridge.apiPost(${endpoint}) 失败，回退 fetch:`, e);
        }
      }
      const options = {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      };
      if (_detectedPrefix) {
        try {
          return await tryFetchJson(`${_detectedPrefix}${endpoint}`, options);
        } catch (e) {}
      }
      const prefixes = [
        `/${PLUGIN_ID}/`,
        `/astrbot_plugin_msg2img/`,
        `/api/plugins/${PLUGIN_ID}/`,
        `/api/plugins/astrbot_plugin_msg2img/`,
        `api/`,
        `./api/`,
        `./`,
      ];
      for (const p of prefixes) {
        try {
          const res = await tryFetchJson(`${p}${endpoint}`, options);
          _detectedPrefix = p;
          return res;
        } catch (e) {}
      }
      throw new Error(`请求后端失败 (${endpoint})`);
    }
  };

  // ---- 预设文案选项库 ----
  const PRESET_TEMPLATES = {
    normal: `# 欢迎使用 AstrBot 消息转图助手
这是一个高颜值的消息转图插件示例！
- 支持 iOS 与 Android 16 双重风格
- 背景随机散落闪烁小星星
- 智能文本折行与美观排版

> 科技让生活更美好，AI 让交互更有温度。

祝您使用愉快！✨`,

    code: `### 🐍 Python 快速示例
以下是消息转图核心处理逻辑：

\`\`\`python
async def render_text_to_image(text: str):
    image = renderer.render(text, style="android16")
    return await save_and_upload(image)
\`\`\`

支持清晰的代码高亮与等宽字体排版。`,

    mosaic: `# 敏感内容半遮蔽演示
这是前半段合规安全的消息内容，可以正常阅览。
这里详细记录了正常的知识与交流事项。

⚠️ 下半段触发了违规敏感词【赌博、诈骗】，系统将自动执行打一半马赛克处理！
请遵守群聊社区规范，共建绿色网络环境。`,

    link: `# 网址链接转图测试
更多详细信息欢迎点击下方链接查看：
- AstrBot 官方文档：https://astrbot.app
- NapCat 核心仓库：https://github.com/NapCatQQ/NapCatQQ
- 项目仓库主页：https://github.com/imsuperone/textimg

可根据设置选择是转图还是保留可点击纯文本。`,
  };

  // ---- Toast 浮窗提示（彻底防重放） ----
  let _lastToastText = "";
  let _lastToastTime = 0;
  function showToast(msg, duration = 3000) {
    const now = Date.now();
    if (_lastToastText === msg && now - _lastToastTime < 1200) {
      return; // 过滤 1.2 秒内完全相同的重复弹窗
    }
    _lastToastText = msg;
    _lastToastTime = now;

    const container = document.getElementById("toastContainer");
    if (!container) return;
    const t = document.createElement("div");
    t.className = "m3-toast";
    t.textContent = msg;
    container.appendChild(t);
    setTimeout(() => {
      t.style.opacity = "0";
      t.style.transform = "translateY(20px)";
      t.style.transition = "all 0.3s";
      setTimeout(() => t.remove(), 300);
    }, duration);
  }

  // ---- 主题切换 ----
  // 注意：页面自身深色/浅色切换与生成的图片配色基调完全独立！
  function applyThemeMode(theme) {
    const targetTheme = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", targetTheme);
    try {
      localStorage.setItem("msg2img_theme", targetTheme);
    } catch (e) {}
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme") || "light";
    const next = current === "light" ? "dark" : "light";
    applyThemeMode(next);
    showToast(`管理台界面已切换为${next === "dark" ? "深色暗黑" : "浅色明亮"}模式`);
  }

  function initTheme() {
    try {
      const saved = localStorage.getItem("msg2img_theme") || "light";
      applyThemeMode(saved);
    } catch (e) {}
  }

  // ---- 分段选择器助手 ----
  function setSegmentedValue(containerId, val) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const items = container.querySelectorAll(".seg-item");
    items.forEach((item) => {
      if (item.getAttribute("data-val") === String(val)) {
        item.classList.add("active");
      } else {
        item.classList.remove("active");
      }
    });
  }

  function getSegmentedValue(containerId, defaultVal = "") {
    const container = document.getElementById(containerId);
    if (!container) return defaultVal;
    const active = container.querySelector(".seg-item.active");
    return active ? active.getAttribute("data-val") : defaultVal;
  }

  function getRadioValue(name, defaultVal = "") {
    const r = document.querySelector(`input[name="${name}"]:checked`);
    return r ? r.value : defaultVal;
  }

  function setRadioValue(name, val) {
    const r = document.querySelector(`input[name="${name}"][value="${val}"]`);
    if (r) r.checked = true;
  }

  // ---- 数据加载与回填 ----
  let currentConfig = {};
  let cachedGroups = [];

  async function loadData() {
    try {
      const res = await api.get("config");
      if (res && res.config) {
        currentConfig = res.config;
        renderConfigToUI(res.config);
      }
      if (res && res.stats) {
        renderStats(res.stats);
      }
    } catch (e) {
      console.warn("[msg2img] 获取配置回退或失败:", e);
      showToast("连接后端失败，显示默认配置");
    }
  }

  function renderStats(stats) {
    const totalEl = document.getElementById("statTotal");
    const vioEl = document.getElementById("statViolations");
    const mosEl = document.getElementById("statMosaic");
    if (totalEl) totalEl.textContent = stats.total_rendered || 0;
    if (vioEl) vioEl.textContent = stats.violations_blocked || 0;
    if (mosEl) mosEl.textContent = stats.mosaic_applied || 0;
  }

  function renderConfigToUI(cfg) {
    const enableEl = document.getElementById("cfgEnable");
    if (enableEl) enableEl.checked = Boolean(cfg.enable ?? true);

    setSegmentedValue("segRenderTrigger", cfg.render_trigger || "always");
    setSegmentedValue("segStyle", cfg.style || "ios");
    setSegmentedValue("segTheme", cfg.theme_mode || "light");

    const starBgEl = document.getElementById("cfgStarBg");
    const starDensityGroup = document.getElementById("starDensityGroup");
    if (starBgEl) {
      starBgEl.checked = Boolean(cfg.star_background ?? true);
      if (starDensityGroup) {
        starDensityGroup.style.display = starBgEl.checked ? "block" : "none";
      }
    }
    setSegmentedValue("segStarDensity", cfg.star_density || "medium");

    const minLenEl = document.getElementById("cfgMinLength");
    if (minLenEl) minLenEl.value = cfg.min_length_threshold || 1;

    setRadioValue("linkMode", cfg.link_mode || "as_image");
    setSegmentedValue("segModerationMode", cfg.moderation_mode || "keywords");
    setRadioValue("violationAction", cfg.violation_action || "mosaic_half");
    setSegmentedValue("segMosaicType", cfg.mosaic_type || "pixel");
    setSegmentedValue("segMosaicHalfPos", cfg.mosaic_half_pos || "bottom");

    // 独立 AI 审查开关
    const aiModSwitch = document.getElementById("cfgEnableAiModeration");
    const aiConfigWrap = document.getElementById("aiConfigWrap");
    const isAiOn = Boolean(cfg.enable_ai_moderation ?? (cfg.moderation_mode === "ai" || cfg.moderation_mode === "both"));
    if (aiModSwitch) {
      aiModSwitch.checked = isAiOn;
    }
    if (aiConfigWrap) {
      aiConfigWrap.style.opacity = isAiOn ? "1" : "0.5";
      aiConfigWrap.style.pointerEvents = isAiOn ? "auto" : "none";
    }

    // half 位置仅 half 模式显示
    const halfPosGroup = document.getElementById("mosaicHalfPosGroup");
    if (halfPosGroup) halfPosGroup.style.display = (cfg.violation_action || "mosaic_half") === "mosaic_half" ? "block" : "none";

    const fontScaleEl = document.getElementById("cfgFontScale");
    const fontScaleVal = document.getElementById("fontScaleVal");
    if (fontScaleEl) {
      fontScaleEl.value = cfg.font_scale || 100;
      if (fontScaleVal) fontScaleVal.textContent = (cfg.font_scale || 100) + "%";
    }

    setSegmentedValue("segImgCompress", cfg.img_compress_level || "medium");

    // 填充与同步多预设词库
    populateKeywordPresets();

    const promptEl = document.getElementById("cfgCustomAiPrompt");
    if (promptEl) {
      promptEl.value = cfg.custom_ai_prompt || (
        "你是一个严格而专业的内容安全审核员。请审查以下文本是否包含违法犯罪、色情低俗、恶意辱骂、暴恐危害、欺诈谣言等违规内容。\n" +
        "请直接输出且仅输出合法的 JSON 格式，严禁添加任何 Markdown 格式或额外解释：\n" +
        '{"violated": true 或 false, "reason": "违规简短原因，无违规填空字符串"}'
      );
    }
    setSegmentedValue("segAiProviderMode", cfg.ai_provider_mode || "astrbot");
    const aiAstrSel=document.getElementById("cfgAiAstrbotModel");
    if(aiAstrSel) aiAstrSel.value=cfg.ai_astrbot_model||"";
    const aiAstrBox=document.getElementById("aiAstrbotBox");
    const aiCustomBox2=document.getElementById("aiCustomBox");
    if(aiAstrBox) aiAstrBox.style.display=(cfg.ai_provider_mode||"astrbot")==="astrbot"?"block":"none";
    if(aiCustomBox2) aiCustomBox2.style.display=(cfg.ai_provider_mode||"astrbot")==="custom"?"block":"none";
    // 延迟拉取模型列表
    setTimeout(()=>fetchAiProviders(), 300);

    const aiBaseEl = document.getElementById("cfgAiBase");
    if (aiBaseEl) aiBaseEl.value = cfg.ai_api_base || "";

    const aiKeyEl = document.getElementById("cfgAiKey");
    if (aiKeyEl) aiKeyEl.value = cfg.ai_api_key || "";

    const aiModelEl = document.getElementById("cfgAiModel");
    if (aiModelEl) aiModelEl.value = cfg.ai_model || "gpt-4o-mini";

    // 默认白名单模式
    const grpMode = cfg.group_mode || "whitelist";
    setSegmentedValue("segGroupMode", grpMode);

    const grpListEl = document.getElementById("cfgGroupList");
    if (grpListEl) grpListEl.value = cfg.group_list || "";

    // 刷新群选择胶囊高亮状态
    updateGroupChipsSelection();
    renderSelectedGroupsFontList();

    // 字体来源
    const fontSrc = cfg.font_source || "auto";
    setSegmentedValue("segFontSource", fontSrc);
    // Emoji 样式（兼容旧 emoji_remote）
    let es = cfg.emoji_style;
    if (!es && typeof cfg.emoji_remote !== "undefined") es = cfg.emoji_remote ? "android" : "none";
    setSegmentedValue("segEmojiStyle", es || "none");
    const guideSeg = document.getElementById("guideSegEmoji");
    if (guideSeg) {
      guideSeg.querySelectorAll(".seg-item").forEach(i=>i.classList.remove("active"));
      const g = guideSeg.querySelector(`.seg-item[data-val="${es||"none"}"]`);
      if (g) g.classList.add("active");
    }

    const cfEl = document.getElementById("cfgCustomFont");
    if (cfEl) cfEl.value = cfg.custom_font_path || "";
    const cbfEl = document.getElementById("cfgCustomBoldFont");
    if (cbfEl) cbfEl.value = cfg.custom_bold_font_path || "";
    const cfUrlEl = document.getElementById("cfgCustomFontUrl");
    if (cfUrlEl) cfUrlEl.value = cfg.custom_font_url || "";
    const customBox = document.getElementById("customFontBox");
    if (customBox) customBox.style.display = fontSrc === "custom" ? "block" : "none";

    // 顶部卡片指示
    const curStyleEl = document.getElementById("statCurrentStyle");
    if (curStyleEl) curStyleEl.textContent = (cfg.style || "ios").toUpperCase();

    const statusBadge = document.getElementById("statStatusBadge");
    if (statusBadge) {
      const isEn = Boolean(cfg.enable ?? true);
      statusBadge.textContent = isEn ? "运行中" : "已暂停";
      statusBadge.className = `widget-badge ${isEn ? "pill-green" : "pill-amber"}`;
    }

    // 动态提示群聊范围状态
    updateGroupModeStatusUI(grpMode, cfg.group_list || "");
  }

  function collectConfigFromUI() {
    const enableEl = document.getElementById("cfgEnable");
    const minLenEl = document.getElementById("cfgMinLength");
    const kwEl = document.getElementById("cfgKeywords");
    const aiBaseEl = document.getElementById("cfgAiBase");
    const aiKeyEl = document.getElementById("cfgAiKey");
    const aiModelEl = document.getElementById("cfgAiModel");
    const grpListEl = document.getElementById("cfgGroupList");
    const starBgEl = document.getElementById("cfgStarBg");
    const cfEl = document.getElementById("cfgCustomFont");
    const cbfEl = document.getElementById("cfgCustomBoldFont");
    const cfUrlEl = document.getElementById("cfgCustomFontUrl");
    const fontScaleEl = document.getElementById("cfgFontScale");

    let minLen = minLenEl ? parseInt(minLenEl.value, 10) || 1 : 1;
    minLen = Math.min(1000, Math.max(1, minLen));

    return {
      enable: enableEl ? enableEl.checked : true,
      render_trigger: getSegmentedValue("segRenderTrigger", "always"),
      style: getSegmentedValue("segStyle", "ios"),
      theme_mode: getSegmentedValue("segTheme", "light"),
      star_background: starBgEl ? starBgEl.checked : true,
      star_density: getSegmentedValue("segStarDensity", "medium"),
      min_length_threshold: minLen,
      link_mode: getRadioValue("linkMode", "as_image"),
      moderation_mode: getSegmentedValue("segModerationMode", "keywords"),
      enable_ai_moderation: Boolean(document.getElementById("cfgEnableAiModeration")?.checked),
      violation_action: getRadioValue("violationAction", "mosaic_half"),
      mosaic_type: getSegmentedValue("segMosaicType", "pixel"),
      mosaic_half_pos: getSegmentedValue("segMosaicHalfPos", "bottom"),
      custom_keywords: (()=>{
        const sel = document.getElementById("keywordPresetSelect");
        const txt = document.getElementById("cfgKeywords");
        const presets = getKeywordPresets();
        const activeKey = sel ? sel.value : (currentConfig.active_keyword_preset || "default");
        if (txt) {
          if (presets[activeKey]) presets[activeKey].keywords = txt.value.trim();
          return txt.value.trim();
        }
        return currentConfig.custom_keywords || "";
      })(),
      active_keyword_preset: document.getElementById("keywordPresetSelect")?.value || (currentConfig.active_keyword_preset || "default"),
      keyword_presets: (()=>{
        const sel = document.getElementById("keywordPresetSelect");
        const txt = document.getElementById("cfgKeywords");
        const presets = getKeywordPresets();
        const activeKey = sel ? sel.value : (currentConfig.active_keyword_preset || "default");
        if (txt && presets[activeKey]) {
          presets[activeKey].keywords = txt.value.trim();
        }
        return presets;
      })(),
      ai_provider_mode: getSegmentedValue("segAiProviderMode","astrbot"),
      ai_astrbot_model: document.getElementById("cfgAiAstrbotModel")?.value.trim() || "",
      ai_api_base: aiBaseEl ? aiBaseEl.value.trim() : "",
      ai_api_key: aiKeyEl ? aiKeyEl.value.trim() : "",
      ai_model: aiModelEl ? aiModelEl.value.trim() || "gpt-4o-mini" : "gpt-4o-mini",
      group_mode: getSegmentedValue("segGroupMode", "whitelist"),
      group_list: grpListEl ? grpListEl.value.trim() : "",
      font_source: getSegmentedValue("segFontSource", "auto"),
      custom_font_path: cfEl ? cfEl.value.trim() : "",
      custom_bold_font_path: cbfEl ? cbfEl.value.trim() : "",
      custom_font_url: cfUrlEl ? cfUrlEl.value.trim() : "",
      font_scale: fontScaleEl ? parseInt(fontScaleEl.value,10) || 100 : 100,
      img_compress_level: getSegmentedValue("segImgCompress", "medium"),
      custom_ai_prompt: document.getElementById("cfgCustomAiPrompt")?.value.trim() || "",
      group_configs: (()=>{
        const curList = (document.getElementById("cfgGroupList")?.value || "").split(/[,;\s]+/).map(s=>s.trim()).filter(Boolean);
        const existing = getGroupConfigs();
        const m = {};
        curList.forEach(gid=>{
          const prev = existing[gid] || {};
          const slider = document.querySelector(`.sg-slider[data-gid="${gid}"]`);
          const numEl = document.querySelector(`.sg-num[data-gid="${gid}"]`);
          const stSel = document.querySelector(`.sg-style[data-gid="${gid}"]`);
          const thSel = document.querySelector(`.sg-theme[data-gid="${gid}"]`);
          const ftSel = document.querySelector(`.sg-font[data-gid="${gid}"]`);
          const kwSel = document.querySelector(`.sg-kw[data-gid="${gid}"]`);

          let scaleVal = null;
          if (numEl && numEl.value !== "") {
            scaleVal = parseInt(numEl.value, 10);
          } else if (slider) {
            scaleVal = parseInt(slider.value, 10);
          } else if (prev.font_scale !== undefined) {
            scaleVal = parseInt(prev.font_scale, 10);
          }

          const styleVal = stSel ? stSel.value : (prev.style || "");
          const themeVal = thSel ? thSel.value : (prev.theme_mode || "");
          const fontVal = ftSel ? ftSel.value : (prev.custom_font_path || "");
          const kwVal = kwSel ? kwSel.value : (prev.keyword_preset || "");

          const item = {};
          if (scaleVal && scaleVal !== 100) item.font_scale = scaleVal;
          if (styleVal) item.style = styleVal;
          if (themeVal) item.theme_mode = themeVal;
          if (fontVal) item.custom_font_path = fontVal;
          if (kwVal) item.keyword_preset = kwVal;

          if (Object.keys(item).length > 0) {
            m[gid] = item;
          }
        });
        return m;
      })(),
      emoji_style: getSegmentedValue("segEmojiStyle", "none"),
      group_font_scales: (()=>{ const m={}; document.querySelectorAll("#selectedGroupsFontList .sg-slider").forEach(s=>{ const gid=s.getAttribute("data-gid"); const v=parseInt(s.value,10)||100; if(v!==100) m[gid]=v; });
        return m; })(),
    };
  }

  let _autoSaveTimer = null;
  function triggerAutoSave() {
    if (_autoSaveTimer) clearTimeout(_autoSaveTimer);
    _autoSaveTimer = setTimeout(() => {
      saveConfig(true); // 静默自动保存（1s 防抖，避免每敲一键就 POST）
    }, 1000);
  }

  let _isSaving = false;
  async function saveConfig(silent = false) {
    if (_isSaving) return;
    _isSaving = true;

    const hint = document.getElementById("autoSaveHint");
    const payload = collectConfigFromUI();
    try {
      if (hint) hint.textContent = "● 保存中…";
      const res = await api.post("config", payload);
      if (res && res.ok) {
        if (!silent) showToast("✅ 已自动保存并生效");
        if (hint) {
          hint.textContent = "● 已自动保存 · 修改即生效";
          setTimeout(() => { if (hint) hint.textContent = "● 自动保存已开启 · 修改即生效"; }, 1800);
        }
        currentConfig = payload;
      } else {
        throw new Error((res && res.error) || "保存返回异常");
      }
    } catch (e) {
      if (hint) hint.textContent = "● 保存失败，请重试";
      if (!silent) showToast("保存提示: " + e.message);
    } finally {
      _isSaving = false;
    }
  }

  async function refreshData() {
    await loadData();
    await fetchFontStatus(true);
    await fetchFontFiles();
    await fetchCuratedFonts();
    await fetchEmojiPacks();
    renderSelectedGroupsFontList();
    showToast("数据已刷新");
  }

  // ==========================================
  // 字体状态查询与手动下载
  // ==========================================
  let _isFetchingFonts = false;
  async function fetchFontStatus(silent = false) {
    const pill = document.getElementById("fontStatusPill");
    const hint = document.getElementById("fontFetchHint");
    const emojiHint = document.getElementById("emojiStorageHint");
    try {
      const res = await api.get("fonts/status");
      const f = (res && res.fonts) || {};
      const base = (f.regular || "").split(/[/\\]/).pop() || "无";
      const bold = (f.bold || "").split(/[/\\]/).pop() || "无";
      if (f.has_cjk) {
        if (pill) {
          pill.textContent = `✅ 中文正常 (${base})`;
          pill.style.background = "var(--m3-status-green-bg)";
          pill.style.color = "var(--m3-status-green)";
        }
        if (hint) hint.textContent = `常规 ${base} · 粗体 ${bold} · 共 ${f.active_count || 0} 个可用`;
      } else {
        if (pill) {
          pill.textContent = "❌ 缺中文字体 (中文将显示方框)";
          pill.style.background = "var(--m3-status-amber-bg)";
          pill.style.color = "var(--m3-status-amber)";
        }
        if (hint) hint.textContent = "请选择一个精选字体下载，或从持久化目录选择";
        if (!silent) showToast("缺中文字体，请先选择字体下载");
      }
      if (emojiHint) {
        const kb = f.emoji_storage_kb || 0;
        const txt = kb >= 1024 ? (kb/1024).toFixed(1)+" MB" : kb+" KB";
        const style = f.emoji_style || "none";
        const label = style==="none" ? "未启用" : style;
        emojiHint.textContent = `占用 ${txt} · 当前 ${label}`;
      }
      // 首次引导：无字体或 emoji 未选则弹出
      if (!silent) checkAndShowGuide(f);
      else {
        // 静默加载也检查是否需要首次引导（仅首次）
        const guided = (()=>{ try{ return localStorage.getItem("msg2img_guided")==="1"; }catch(e){ return true; }})();
        if (!guided && (!f.has_cjk || (f.emoji_style||"none")==="none")) checkAndShowGuide(f);
      }
    } catch (e) {
      if (pill) pill.textContent = "字体状态未知";
      if (!silent) showToast("查询字体状态: " + e.message);
    }
  }

  async function downloadFonts() {
    if (_isFetchingFonts) return;
    _isFetchingFonts = true;
    const hint = document.getElementById("fontFetchHint");
    const btn = document.getElementById("fetchFontsBtn");
    try {
      if (btn) btn.disabled = true;
      if (hint) hint.textContent = "正在下载（中文字体约 10MB、全彩 Emoji 约 24MB，请稍候）…";
      const res = await api.post("fonts/download");
      if (res && res.downloaded && res.downloaded.length) {
        showToast(`✅ 下载完成: ${res.downloaded.join(", ")}`);
      } else if (res && res.ok) {
        showToast("✅ 已就绪，无需下载");
      } else {
        showToast("下载提示: " + ((res && res.error) || "未知"));
      }
      await fetchFontStatus(true);
      await fetchFontFiles();
    } catch (e) {
      showToast("字体下载: " + e.message);
    } finally {
      _isFetchingFonts = false;
      if (btn) btn.disabled = false;
    }
  }

  // ==========================================
  // 持久化目录字体列表与删除
  // ==========================================
  async function fetchFontFiles() {
    const box = document.getElementById("fontFilesBox");
    const sel = document.getElementById("fontSelectDropdown");
    try {
      const res = await api.get("fonts/files");
      const files = (res && res.files) || [];
      if (!files.length) {
        if (box) box.innerHTML = `<div class="font-files-empty">目录为空（系统字体够用，或尚未下载）</div>`;
        if (sel) {
          sel.innerHTML = `<option value="">-- 暂无已安装字体 --</option>`;
        }
        return;
      }
      if (box) {
        box.innerHTML = "";
        files.forEach((f) => {
        const row = document.createElement("div");
        row.className = "font-file-row";
        const sizeTxt = f.size_kb >= 1024
          ? `${(f.size_kb / 1024).toFixed(1)} MB`
          : `${f.size_kb} KB`;
        const fnameSafe = escapeHtml(f.name);
        row.innerHTML =
          `<span class="font-file-icon">🔤</span>` +
          `<span class="fname" title="${fnameSafe}">${fnameSafe}</span>` +
          `<span class="fsize">${sizeTxt}${f.usable ? "" : " · 异常"}</span>` +
          `<button type="button" class="font-file-del font-del-btn" data-fname="${fnameSafe}" title="删除该字体">删除</button>`;
        box.appendChild(row);
        });
      }
      if (sel) {
        const cur = currentConfig.custom_font_path || "";
        sel.innerHTML = `<option value="">-- 请选择已安装字体 --</option>`;
        files.forEach((f) => {
          const opt = document.createElement("option");
          opt.value = f.name;
          opt.textContent = `${f.name} (${f.size_kb >= 1024 ? (f.size_kb/1024).toFixed(1)+" MB" : f.size_kb+" KB"}${f.usable ? "" : " · 异常"})`;
          if (cur && (cur === f.name || cur.endsWith("/"+f.name) || cur.endsWith("\\"+f.name))) opt.selected = true;
          sel.appendChild(opt);
        });
      }
      populateGroupFontSelects();
    } catch (e) {
      if (box) box.innerHTML = `<div class="font-files-empty">读取失败: ${escapeHtml(e.message)}</div>`;
    }
  }

  async function applySelectedFont() {
    const sel = document.getElementById("fontSelectDropdown");
    if (!sel || !sel.value) {
      showToast("请先选择一个已安装字体");
      return;
    }
    const fname = sel.value;
    // 直接通过配置切换：设为 custom 并指向该文件
    try {
      showToast(`🔤 正在切换到 ${fname}…`);
      // 复用保存逻辑：更新 UI 中的隐藏配置并触发保存
      const cfEl = document.getElementById("cfgCustomFont");
      if (cfEl) cfEl.value = fname;
      const srcSeg = document.getElementById("segFontSource");
      if (srcSeg) {
        setSegmentedValue("segFontSource", "custom");
        const customBox = document.getElementById("customFontBox");
        if (customBox) customBox.style.display = "block";
      }
      await saveConfig(false);
      await fetchFontStatus(true);
      await fetchCuratedFonts();
      await fetchFontFiles();
      // 触发预览刷新
      setTimeout(() => triggerPreview(), 400);
    } catch (e) {
      showToast("切换失败: " + e.message);
    }
  }

  async function downloadCustomUrl() {
    const input = document.getElementById("cfgCustomFontUrl");
    const url = input ? input.value.trim() : "";
    if (!url) {
      showToast("请先粘贴字体直链");
      return;
    }
    if (!/\.(ttf|ttc|otf)(\?.*)?$/i.test(url)) {
      showToast("直链需指向 .ttf/.ttc/.otf 文件");
      return;
    }
    try {
      showToast("⬇️ 正在下载字体，请稍候…");
      // 先将 URL 写入配置，使后端下载逻辑能读取到
      const payload = collectConfigFromUI();
      payload.custom_font_url = url;
      // 保存配置会触发后端 after-save 的自动下载任务，但为即时反馈直接调用下载接口
      await api.post("config", payload);
      currentConfig.custom_font_url = url;
      const res = await api.post("fonts/download");
      if (res && res.downloaded && res.downloaded.length) {
        showToast(`✅ 下载完成: ${res.downloaded.join(", ")}`);
        // 下载后若为 custom 模式，自动切换到新文件
        const fname = url.split("?")[0].split("/").pop();
        const cfEl = document.getElementById("cfgCustomFont");
        if (cfEl && fname) cfEl.value = fname;
        setSegmentedValue("segFontSource", "custom");
        const customBox = document.getElementById("customFontBox");
        if (customBox) customBox.style.display = "block";
        await api.post("config", collectConfigFromUI());
      } else if (res && res.ok) {
        showToast("✅ 已就绪");
      } else {
        showToast("下载提示: " + ((res && res.error) || "未知"));
      }
      await fetchFontStatus(true);
      await fetchFontFiles();
      await fetchCuratedFonts();
      setTimeout(() => triggerPreview(), 400);
    } catch (e) {
      showToast("下载失败: " + e.message);
    }
  }

  // 首次引导
  function checkAndShowGuide(f) {
    try {
      if (localStorage.getItem("msg2img_guided")==="1") return;
    } catch(e){ return; }
    const needFont = !f.has_cjk;
    const needEmoji = (f.emoji_style||"none")==="none";
    if (!needFont && !needEmoji) return;
    const modal = document.getElementById("firstGuideModal");
    const box = document.getElementById("guideFontBox");
    if (modal && box) {
      // 克隆精选列表到引导
      const src = document.getElementById("curatedFontsBox");
      if (src) box.innerHTML = src.innerHTML;
      // 去除按钮禁用，让引导可下载
      box.querySelectorAll("button[disabled]").forEach(b=>b.removeAttribute("disabled"));
      modal.style.display = "flex";
    }
  }
  function hideGuide(){ const m=document.getElementById("firstGuideModal"); if(m) m.style.display="none"; try{ localStorage.setItem("msg2img_guided","1"); }catch(e){} }
  async function guideConfirm(){
    const sel = document.querySelector("#guideFontBox .curated-font-row.ready, #curatedFontsBox .curated-font-row.ready");
    // 若引导内未选，尝试取第一个已下载，否则提示
    const guideSel = document.querySelector("#guideFontBox .curated-install-btn:not([disabled])");
    const emojiVal = document.querySelector("#guideSegEmoji .seg-item.active")?.getAttribute("data-val") || "android";
    // 先应用 emoji
    setSegmentedValue("segEmojiStyle", emojiVal);
    // 若有已下载字体，自动应用第一个
    const readyBtn = document.querySelector("#curatedFontsBox .curated-font-row.ready .curated-install-btn");
    // 引导确认仅保存 emoji，若字体已下载则提示去持久化选择
    await saveConfig(false);
    hideGuide();
    showToast("✅ 已保存引导选择，可在持久化目录选择字体");
    await fetchFontStatus(true);
  }

  let _deletingFont = "";
  async function deleteFont(name) {
    if (!name || _deletingFont) return;
    if (!window.confirm(`确定删除字体「${name}」吗？`)) return;
    _deletingFont = name;
    try {
      showToast(`正在删除 ${name}…`);
      const res = await api.post("fonts/delete", { name });
      if (res && res.ok) {
        showToast(`🗑️ 已删除 ${name}`);
      } else {
        showToast("删除提示: " + ((res && res.error) || "未知错误"));
      }
      await fetchFontStatus(true);
      await fetchFontFiles();
      await fetchCuratedFonts();
    } catch (e) {
      showToast("删除字体异常: " + e.message);
    } finally {
      _deletingFont = "";
    }
  }

  // 精选字体一键下载与卸载
  async function fetchCuratedFonts() {
    const box = document.getElementById("curatedFontsBox");
    if (!box) return;
    try {
      const res = await api.get("fonts/curated");
      const list = (res && res.curated) || [];
      if (!list.length) {
        box.innerHTML = `<div class="font-files-empty">精选列表加载失败</div>`;
        return;
      }
      box.innerHTML = "";
      list.forEach((it) => {
        const row = document.createElement("div");
        row.className = `curated-font-row ${it.ready ? "ready" : ""}`;
        const badge = it.ready ? "已下载" : "未下载";
        const cls = it.ready ? "badge-ready" : "badge-idle";
        const btnText = it.ready ? "重新下载" : "下载";
        const btnCls = it.ready ? "secondary-btn" : "primary-btn";
        let btns = `<button type="button" class="m3-btn ${btnCls} curated-install-btn" data-id="${escapeHtml(it.id)}">${btnText}</button>`;
        if (it.ready) {
          btns = `<button type="button" class="font-file-del curated-delete-btn" data-cid="${escapeHtml(it.id)}" title="从持久化目录删除该字体包" style="margin-right:6px;">卸载</button>` + btns;
        }
        row.innerHTML =
          `<div class="curated-info"><span class="curated-name">${escapeHtml(it.name)}</span>` +
          `<span class="curated-desc">${escapeHtml(it.desc)}</span></div>` +
          `<span class="curated-badge ${cls}">${badge}</span>` +
          `<div style="display:flex; align-items:center;">${btns}</div>`;
        box.appendChild(row);
      });
    } catch (e) {
      box.innerHTML = `<div class="font-files-empty">精选列表读取失败: ${escapeHtml(e.message)}</div>`;
    }
  }

  function getGroupConfigs() {
    let raw = currentConfig.group_configs;
    if (typeof raw === "string") { try { raw = JSON.parse(raw||"{}"); } catch(e){ raw={}; } }
    return (raw && typeof raw==="object") ? raw : {};
  }
  function getGroupScales() {
    const cfgs = getGroupConfigs();
    let raw = currentConfig.group_font_scales;
    if (typeof raw === "string") { try { raw = JSON.parse(raw||"{}"); } catch(e){ raw={}; } }
    const res = (raw && typeof raw==="object") ? Object.assign({}, raw) : {};
    Object.keys(cfgs).forEach(k=>{ if(cfgs[k] && cfgs[k].font_scale) res[k]=cfgs[k].font_scale; });
    return res;
  }

  function getKeywordPresets() {
    let p = currentConfig.keyword_presets;
    if (typeof p === "string") {
      try { p = JSON.parse(p); } catch(e) { p = {}; }
    }
    if (!p || typeof p !== "object" || Object.keys(p).length === 0) {
      p = {
        default: { name: "标准涉敏与违禁词库 (默认综合)", keywords: currentConfig.custom_keywords || "" }
      };
    }
    return p;
  }

  function populateKeywordPresets() {
    const sel = document.getElementById("keywordPresetSelect");
    const txt = document.getElementById("cfgKeywords");
    if (!sel) return;
    const presets = getKeywordPresets();
    const activeKey = currentConfig.active_keyword_preset || "default";
    sel.innerHTML = "";
    Object.keys(presets).forEach((k) => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = (presets[k] && presets[k].name) ? presets[k].name : k;
      if (k === activeKey) opt.selected = true;
      sel.appendChild(opt);
    });
    if (txt) {
      const activeObj = presets[activeKey] || presets["default"] || Object.values(presets)[0] || {};
      txt.value = (activeObj.keywords || currentConfig.custom_keywords || "").trim();
    }
  }

  function populateGroupKeywordSelects() {
    const presets = getKeywordPresets();
    const grpCfgs = getGroupConfigs();
    document.querySelectorAll(".sg-kw").forEach((sel) => {
      const gid = sel.getAttribute("data-gid");
      const cur = (grpCfgs[gid] && grpCfgs[gid].keyword_preset) || "";
      sel.innerHTML = `<option value="">跟随全局 (默认)</option>`;
      Object.keys(presets).forEach((k) => {
        const opt = document.createElement("option");
        opt.value = k;
        opt.textContent = (presets[k] && presets[k].name) ? presets[k].name : k;
        if (cur === k) opt.selected = true;
        sel.appendChild(opt);
      });
    });
  }

  function renderSelectedGroupsFontList() {
    const box = document.getElementById("selectedGroupsFontList");
    const wrap = document.getElementById("selectedGroupsBox");
    if (!box || !wrap) return;
    const curList = (document.getElementById("cfgGroupList")?.value || "").split(/[,;\s]+/).map(s=>s.trim()).filter(Boolean);
    const grpCfgs = getGroupConfigs();
    if (!curList.length) { wrap.style.display="none"; box.innerHTML=""; return; }
    wrap.style.display="block";
    box.innerHTML="";
    curList.forEach(gid=>{
      const nameObj = cachedGroups.find(g=>String(g.gid)===gid);
      const gname = nameObj ? (nameObj.group_name||gid) : gid;
      const c = grpCfgs[gid] || {};
      const hasCustomScale = c.font_scale !== undefined && c.font_scale !== null && c.font_scale !== "";
      const sc = parseInt(hasCustomScale ? c.font_scale : (currentConfig.font_scale || 100), 10) || 100;
      const curStyle = c.style || "";
      const curTheme = c.theme_mode || "";
      const curFont = c.custom_font_path || "";
      const curKw = c.keyword_preset || "";
      const globalStyleName = (currentConfig.style || "ios").toUpperCase();
      const globalThemeName = (currentConfig.theme_mode === "dark") ? "深色" : "浅色";

      const row=document.createElement("div");
      row.className="selected-group-card";
      row.innerHTML=
        `<div class="sg-card-head">` +
        `  <span class="sg-name" title="${escapeHtml(gname)}">👥 ${escapeHtml(gname)} <span style="opacity:0.6; font-size:11px">(${gid})</span></span>` +
        `  <div class="sg-head-actions">` +
        `    <span class="sg-val" data-gid="${escapeHtml(gid)}">${hasCustomScale ? sc + "%" : sc + "% (全局)"}</span>` +
        `    <button type="button" class="sg-reset-btn" data-gid="${escapeHtml(gid)}" title="恢复此群全部设置至跟随全局默认">恢复默认</button>` +
        `  </div>` +
        `</div>` +
        `<div class="sg-controls">` +
        `  <div class="sg-item sg-item-scale">` +
        `    <div class="sg-label-row"><label>字体大小</label><button type="button" class="sg-mini-reset" data-gid="${escapeHtml(gid)}">默认</button></div>` +
        `    <div style="display:flex; align-items:center; gap:8px;">` +
        `      <input type="range" class="sg-slider" min="50" max="500" step="5" value="${sc}" data-gid="${escapeHtml(gid)}" style="flex:1;">` +
        `      <input type="number" class="m3-input sg-num" min="50" max="500" step="5" value="${sc}" data-gid="${escapeHtml(gid)}" style="width:68px; padding:4px 6px; font-size:12px; font-weight:700; text-align:center;">` +
        `    </div>` +
        `  </div>` +
        `  <div class="sg-item">` +
        `    <label>视觉风格</label>` +
        `    <select class="sg-select sg-style" data-gid="${escapeHtml(gid)}">` +
        `      <option value=""${!curStyle ? " selected" : ""}>跟随全局 (${globalStyleName})</option>` +
        `      <option value="ios"${curStyle === "ios" ? " selected" : ""}>iOS 磨砂玻璃</option>` +
        `      <option value="android16"${curStyle === "android16" ? " selected" : ""}>Android 16 M3</option>` +
        `    </select>` +
        `  </div>` +
        `  <div class="sg-item">` +
        `    <label>配色主题</label>` +
        `    <select class="sg-select sg-theme" data-gid="${escapeHtml(gid)}">` +
        `      <option value=""${!curTheme ? " selected" : ""}>跟随全局 (${globalThemeName})</option>` +
        `      <option value="light"${curTheme === "light" ? " selected" : ""}>浅色明亮</option>` +
        `      <option value="dark"${curTheme === "dark" ? " selected" : ""}>深色暗黑</option>` +
        `    </select>` +
        `  </div>` +
        `  <div class="sg-item">` +
        `    <label>字体设置</label>` +
        `    <select class="sg-select sg-font" data-gid="${escapeHtml(gid)}">` +
        `      <option value="">跟随全局 (默认)</option>` +
        `    </select>` +
        `  </div>` +
        `  <div class="sg-item">` +
        `    <label>审查词库</label>` +
        `    <select class="sg-select sg-kw" data-gid="${escapeHtml(gid)}">` +
        `      <option value="">跟随全局 (默认)</option>` +
        `    </select>` +
        `  </div>` +
        `</div>`;
      box.appendChild(row);
    });
    // 填充群字体与词库下拉
    populateGroupFontSelects();
    populateGroupKeywordSelects();
  }

  function populateGroupFontSelects() {
    const mainSel = document.getElementById("fontSelectDropdown");
    if (!mainSel) return;
    const opts = Array.from(mainSel.options).map(o=>({value:o.value, text:o.textContent}));
    const grpCfgs = getGroupConfigs();
    document.querySelectorAll(".sg-font").forEach(sel=>{
      const gid = sel.getAttribute("data-gid");
      const cur = (grpCfgs[gid] && grpCfgs[gid].custom_font_path) || "";
      sel.innerHTML = `<option value="">全局默认</option>`;
      opts.forEach(o=>{
        if(!o.value) return;
        const opt=document.createElement("option");
        opt.value=o.value; opt.textContent=o.text;
        if(cur && (cur===o.value || cur.endsWith("/"+o.value) || cur.endsWith("\\"+o.value))) opt.selected=true;
        sel.appendChild(opt);
      });
    });
  }
  async function fetchEmojiPacks() {
    const box=document.getElementById("emojiPacksBox");
    if(!box) return;
    try{
      const res=await api.get("emoji/packs");
      const packs=res&&res.packs||[];
      const statusRes=await api.get("fonts/status").catch(()=>({fonts:{}}));
      const curStyle=(statusRes&&statusRes.fonts&&statusRes.fonts.emoji_style)||"none";
      const totalKb=(statusRes&&statusRes.fonts&&statusRes.fonts.emoji_storage_kb)||0;
      const totalTxt=totalKb>=1024?(totalKb/1024).toFixed(1)+" MB":totalKb+" KB";
      if(!packs.length){
        box.innerHTML=`<div class="font-files-empty">暂无 Emoji 样式</div>`;
        return;
      }
      box.innerHTML="";
      // 总占用
      const head=document.createElement("div");
      head.className="font-files-empty";
      head.textContent=`当前应用：${curStyle==="none"?"未启用":curStyle} · 总占用 ${totalTxt}`;
      box.appendChild(head);
      packs.forEach(p=>{
        const isActive=curStyle===p.id;
        const installed=!!p.installed;
        const storageKb=p.storage_kb||0;
        const storageTxt=storageKb>=1024?(storageKb/1024).toFixed(1)+" MB":storageKb+" KB";
        const row=document.createElement("div");
        row.className=`font-file-row ${isActive?"active":""}`;
        const badge=isActive?"✓ 使用中":installed?"已下载":"未下载";
        const bcls=isActive?"badge-active":installed?"badge-ready":"badge-idle";
        let btns = "";
        if (installed) {
          btns = `<button type="button" class="font-file-del emoji-del-btn" data-emoji-del="${p.id}" title="删除此样式资源">删除</button>`;
        } else {
          btns = `<button type="button" class="m3-btn primary-btn emoji-dl-btn" data-emoji-dl="${p.id}" style="padding:4px 12px; font-size:12px;">下载</button>`;
        }
        row.innerHTML=`<span class="font-file-icon">${p.id==="ios"?"🍎":p.id==="android"?"🤖":"🪟"}</span><span class="fname">${escapeHtml(p.name)}</span><span class="fsize">${escapeHtml(p.desc)} · ${storageTxt}</span><span class="curated-badge ${bcls}">${badge}</span>${btns}`;
        box.appendChild(row);
      });
    }catch(e){
      box.innerHTML=`<div class="font-files-empty">读取失败: ${escapeHtml(e.message)}</div>`;
    }
  }
  async function downloadEmojiPack(style){
    try{
      showToast("⬇️ 正在下载 Emoji "+style+"…");
      const res=await api.post("emoji/download", {style});
      if(res&&res.ok) {
        showToast("✅ Emoji 下载完成，已可选中");
        setSegmentedValue("segEmojiStyle", style);
        triggerAutoSave();
        triggerPreview();
      } else {
        showToast("下载提示: "+((res&&res.error)||"未知"));
      }
      await fetchEmojiPacks();
      await fetchFontStatus(true);
    }catch(e){ showToast("下载失败: "+e.message); }
  }
  async function deleteEmojiPack(style){
    if(!confirm(`确定删除 Emoji 样式 ${style} 的下载缓存吗？`)) return;
    try{
      showToast("正在删除 Emoji 缓存…");
      const res=await api.post("emoji/delete", {style});
      if(res&&res.ok) {
        showToast("🗑️ 已删除 "+style);
        // 如果当前选中的是被删样式，重置为 none
        if ((currentConfig.emoji_style || "none") === style) {
          setSegmentedValue("segEmojiStyle", "none");
          currentConfig.emoji_style = "none";
          triggerAutoSave();
        }
      }
      else showToast("删除提示: "+((res&&res.error)||"未知"));
      await fetchEmojiPacks();
      await fetchFontStatus(true);
    }catch(e){ showToast("删除失败: "+e.message); }
  }
  async function fetchAiProviders(){
    const sel=document.getElementById("cfgAiAstrbotModel");
    if(!sel) return;
    try{
      const res=await api.get("ai/providers");
      const list=res&&res.providers||[];
      const cur=currentConfig.ai_astrbot_model||"";
      sel.innerHTML=`<option value="">-- 使用默认模型 --</option>`;
      list.forEach(m=>{
        const opt=document.createElement("option");
        opt.value=m; opt.textContent=m;
        if(m===cur) opt.selected=true;
        sel.appendChild(opt);
      });
      if(list.length===0){
        sel.innerHTML=`<option value="">-- 暂无可用模型 --</option>`;
      }
    }catch(e){
      // 静默
    }
  }

  let _installingCurated = "";
  async function installCuratedFont(cid) {
    if (!cid || _installingCurated) return;
    _installingCurated = cid;
    try {
      showToast("⬇️ 正在下载字体，请稍候…");
      const res = await api.post("fonts/curated_install", { id: cid });
      if (res && res.ok) {
        showToast(`✅ ${cid} 已下载并切换生效`);
      } else {
        showToast("下载提示: " + ((res && res.error) || "未知"));
      }
      await fetchFontStatus(true);
      await fetchFontFiles();
      await fetchCuratedFonts();
    } catch (e) {
      showToast("下载字体: " + e.message);
    } finally {
      _installingCurated = "";
    }
  }

  // ==========================================
  // 主动拉取与勾选群聊功能
  // ==========================================
  let _isFetchingGroups = false;
  async function fetchGroups() {
    if (_isFetchingGroups) return;
    _isFetchingGroups = true;

    const hintEl = document.getElementById("groupFetchHint");
    const boxEl = document.getElementById("groupPickerBox");
    const btnEl = document.getElementById("fetchGroupsBtn");

    try {
      if (btnEl) btnEl.disabled = true;
      if (hintEl) hintEl.textContent = "正在拉取机器人所有群聊...";

      let res;
      try {
        res = await api.post("groups/fetch");
      } catch (e) {
        res = await api.get("groups");
      }

      if (res && Array.isArray(res.groups)) {
        cachedGroups = res.groups;
        if (cachedGroups.length === 0) {
          if (hintEl) hintEl.textContent = "未探测到群聊，已在后台自动监听新消息群";
          showToast("暂未探测到群聊，发送群消息后即可自动识别");
        } else {
          if (hintEl) hintEl.textContent = `已获取 ${cachedGroups.length} 个群聊，点击直接勾选/取消勾选`;
          showToast(`✅ 已获取到 ${cachedGroups.length} 个群聊`);
        }
        renderGroupChips(cachedGroups);
        if (boxEl) boxEl.style.display = "flex";
      } else {
        throw new Error("返回群列表格式不符");
      }
    } catch (e) {
      if (hintEl) hintEl.textContent = "拉取群聊失败: " + e.message;
      showToast("拉取群聊: " + e.message);
    } finally {
      _isFetchingGroups = false;
      if (btnEl) btnEl.disabled = false;
    }
  }

  function renderGroupChips(groups) {
    const boxEl = document.getElementById("groupPickerBox");
    if (!boxEl) return;
    boxEl.innerHTML = "";

    const curList = (document.getElementById("cfgGroupList")?.value || "")
      .split(/[,;\s]+/)
      .map((g) => g.trim())
      .filter(Boolean);

    groups.forEach((g) => {
      const isSelected = curList.includes(String(g.gid));
      const chip = document.createElement("div");
      chip.className = `group-select-chip ${isSelected ? "selected" : ""}`;
      chip.setAttribute("data-gid", g.gid);
      chip.innerHTML = `
        <span>👥</span>
        <span>${escapeHtml(g.group_name || g.gid)}</span>
        <span style="opacity: 0.6; font-size: 11px;">(${g.gid})</span>
      `;
      boxEl.appendChild(chip);
    });
  }

  function updateGroupChipsSelection() {
    const curList = (document.getElementById("cfgGroupList")?.value || "")
      .split(/[,;\s]+/)
      .map((g) => g.trim())
      .filter(Boolean);

    document.querySelectorAll(".group-select-chip").forEach((chip) => {
      const gid = chip.getAttribute("data-gid");
      if (curList.includes(gid)) {
        chip.classList.add("selected");
      } else {
        chip.classList.remove("selected");
      }
    });
    // 同步刷新下方选中的群列表滑块
    renderSelectedGroupsFontList();
  }

  function toggleGroupSelection(gid) {
    const input = document.getElementById("cfgGroupList");
    if (!input) return;
    let list = input.value
      .split(/[,;\s]+/)
      .map((g) => g.trim())
      .filter(Boolean);

    if (list.includes(gid)) {
      list = list.filter((g) => g !== gid);
    } else {
      list.push(gid);
    }
    input.value = list.join(", ");
    updateGroupChipsSelection();
    renderSelectedGroupsFontList();
    updateGroupModeStatusUI(getSegmentedValue("segGroupMode", "whitelist"), input.value);
    triggerAutoSave(); // 实时即刻保存生效
  }

  function updateGroupModeStatusUI(mode, groupListStr) {
    const pill = document.getElementById("groupStatusPill");
    const note = document.getElementById("groupHelpNote");
    const list = (groupListStr || "").split(/[,;\s]+/).filter(Boolean);

    if (mode === "all") {
      if (pill) {
        pill.textContent = "🌐 全局生效 (所有群聊均会转图)";
        pill.style.background = "var(--m3-status-cyan-bg)";
        pill.style.color = "var(--m3-status-cyan)";
      }
      if (note) {
        note.textContent = "✅ 当前已开启全群生效，机器人接收到任何群的文本回复均会自动转为美图。";
        note.style.color = "var(--m3-status-green)";
      }
    } else if (mode === "blacklist") {
      if (pill) {
        pill.textContent = `🚫 黑名单模式 (${list.length} 个群排除)`;
        pill.style.background = "var(--m3-status-amber-bg)";
        pill.style.color = "var(--m3-status-amber)";
      }
      if (note) {
        note.textContent = `除黑名单中的 ${list.length} 个群外，其他群聊均会正常转图。`;
        note.style.color = "var(--m3-sys-color-outline)";
      }
    } else {
      // whitelist
      if (list.length === 0) {
        if (pill) {
          pill.textContent = "🔒 群聊未开启 (当前仅私聊有效)";
          pill.style.background = "var(--m3-status-amber-bg)";
          pill.style.color = "var(--m3-status-amber)";
        }
        if (note) {
          note.textContent = "⚠️ 当前白名单未添加任何群号！机器人所有群聊回复均保持纯文本，不会转为图片。请点击上方按钮拉取并勾选群聊！";
          note.style.color = "var(--m3-status-amber)";
        }
      } else {
        if (pill) {
          pill.textContent = `✅ 白名单已启用 (已指定 ${list.length} 个生效群)`;
          pill.style.background = "var(--m3-status-green-bg)";
          pill.style.color = "var(--m3-status-green)";
        }
        if (note) {
          note.textContent = `✅ 已指定 ${list.length} 个群聊生效：[${list.join(", ")}]，这些群的机器人文本将自动渲染为图片。`;
          note.style.color = "var(--m3-status-green)";
        }
      }
    }
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ==========================================
  // 仅选择模板填入输入框（不自动触发生成）
  // ==========================================
  let _activeMosaicMode = "none";

  function applyPreset(tplKey) {
    const inputEl = document.getElementById("previewInput");
    if (PRESET_TEMPLATES[tplKey]) {
      if (inputEl) inputEl.value = PRESET_TEMPLATES[tplKey];
      _activeMosaicMode = tplKey === "mosaic" ? "half" : "none";

      // 视觉高亮选中的选项胶囊
      document.querySelectorAll(".m3-chip").forEach((chip) => {
        if (chip.getAttribute("data-tpl") === tplKey) {
          chip.classList.add("selected");
        } else {
          chip.classList.remove("selected");
        }
      });
      showToast(`已载入「${tplKey}」文案，请点击实时生成预览`);
    }
  }

  // ==========================================
  // 实时生成交互预览（使用轻量 Base64，彻底秒显不卡死）
  // ==========================================
  let _isPreviewing = false;
  async function triggerPreview() {
    if (_isPreviewing) return;
    _isPreviewing = true;

    const inputEl = document.getElementById("previewInput");
    let text = inputEl ? inputEl.value.trim() : "";
    if (!text) {
      text = PRESET_TEMPLATES.normal;
      if (inputEl) inputEl.value = text;
    }

    const starBgEl = document.getElementById("cfgStarBg");
    const themeMode = getSegmentedValue("segTheme", "light");
    const styleMode = getSegmentedValue("segStyle", "ios");

    // 后端优先使用 text 明文；不再双发 text_base64（大文本体积减半）
    const payload = {
      text: text,
      style: styleMode,
      theme_mode: themeMode,
      star_background: starBgEl ? starBgEl.checked : true,
      star_density: getSegmentedValue("segStarDensity", "medium"),
      mosaic_mode: _activeMosaicMode,
      mosaic_type: getSegmentedValue("segMosaicType", "pixel"),
      mosaic_half_pos: getSegmentedValue("segMosaicHalfPos", "bottom"),
      font_scale: parseInt(document.getElementById("cfgFontScale")?.value || "100",10) || 100,
      emoji_style: getSegmentedValue("segEmojiStyle", "none"),
    };

    const spinner = document.getElementById("previewSpinner");
    const previewBtn = document.getElementById("renderPreviewBtn");
    const previewImg = document.getElementById("previewImage");
    const placeholder = document.getElementById("previewEmptyPlaceholder");
    const metaBox = document.getElementById("previewMeta");
    const dimEl = document.getElementById("prevDimensions");
    const styleEl = document.getElementById("prevStyle");
    const latEl = document.getElementById("prevLatency");

    try {
      const t0 = performance.now();
      if (spinner) spinner.style.display = "inline-block";
      if (previewBtn) previewBtn.disabled = true;

      const res = await api.post("preview", payload);
      const elapsed = Math.round(performance.now() - t0);

      if (res && res.image_base64) {
        if (previewImg) {
          previewImg.onload = () => {
            if (placeholder) placeholder.style.display = "none";
            previewImg.style.display = "block";
          };
          previewImg.src = res.image_base64;
          if (placeholder) placeholder.style.display = "none";
          previewImg.style.display = "block";
        }
        if (metaBox) metaBox.style.display = "flex";
        if (dimEl) dimEl.textContent = `${res.width} × ${res.height}`;
        if (styleEl) styleEl.textContent = `${styleMode.toUpperCase()}${themeMode === "dark" ? " (深色)" : ""}${_activeMosaicMode !== "none" ? " (半马赛克)" : ""}`;
        if (latEl) latEl.textContent = `${elapsed}ms`;
        showToast("✨ 预览图片生成成功");
      } else {
        throw new Error((res && res.error) || "后端未返回图片数据");
      }
    } catch (e) {
      showToast("生成预览图: " + e.message);
    } finally {
      _isPreviewing = false;
      if (spinner) spinner.style.display = "none";
      if (previewBtn) previewBtn.disabled = false;
    }
  }

  let _lastActiveTab = "visual";

  function switchCategoryTab(tabId) {
    if (tabId !== "preview") {
      _lastActiveTab = tabId;
    }
    document.querySelectorAll(".cat-tab").forEach((t) => {
      if (t.getAttribute("data-tab") === tabId) {
        t.classList.add("active");
      } else {
        t.classList.remove("active");
      }
    });

    const workspace = document.getElementById("mainWorkspace");
    const isMobile = window.innerWidth <= 900;

    if (tabId === "preview") {
      if (isMobile) {
        if (workspace) workspace.classList.add("mobile-view-preview");
        const mobBtn = document.getElementById("mobileFloatPreviewBtn");
        if (mobBtn) mobBtn.innerHTML = `<span>⚙️ 返回配置</span>`;
      } else {
        const prevCol = document.getElementById("previewColumn");
        if (prevCol) {
          prevCol.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      }
    } else {
      if (workspace) workspace.classList.remove("mobile-view-preview");
      const mobBtn = document.getElementById("mobileFloatPreviewBtn");
      if (mobBtn) mobBtn.innerHTML = `<span>👁️ 实时预览</span>`;

      document.querySelectorAll(".settings-section").forEach((sec) => {
        if (sec.getAttribute("data-section") === tabId) {
          sec.classList.add("active");
        } else {
          sec.classList.remove("active");
        }
      });
    }
  }

  // ==========================================
  // 全局唯一事件分发器（彻底避免重复绑定与冒泡冲突）
  // ==========================================
  function bindGlobalDelegation() {
    document.addEventListener("click", function (e) {
      // 0. 分类 Tab 切换
      const catTab = e.target.closest(".cat-tab");
      if (catTab) {
        e.preventDefault();
        const tabId = catTab.getAttribute("data-tab");
        if (tabId) switchCategoryTab(tabId);
        return;
      }

      // 0.1 移动端浮动预览按钮
      if (e.target.closest("#mobileFloatPreviewBtn")) {
        e.preventDefault();
        const workspace = document.getElementById("mainWorkspace");
        const isPreviewActive = workspace && workspace.classList.contains("mobile-view-preview");
        if (isPreviewActive) {
          switchCategoryTab(_lastActiveTab || "visual");
        } else {
          switchCategoryTab("preview");
        }
        return;
      }

      // 0.2 单群恢复默认
      const grpResetBtn = e.target.closest(".sg-reset-btn");
      if (grpResetBtn) {
        e.preventDefault();
        const gid = grpResetBtn.getAttribute("data-gid");
        if (gid) {
          const cfgs = getGroupConfigs();
          delete cfgs[gid];
          currentConfig.group_configs = cfgs;
          renderSelectedGroupsFontList();
          triggerAutoSave();
          showToast(`已将群 ${gid} 恢复为跟随全局默认设置`);
        }
        return;
      }

      // 0.3 单群字体大小设为默认
      const grpResetScaleBtn = e.target.closest(".sg-mini-reset");
      if (grpResetScaleBtn) {
        e.preventDefault();
        const gid = grpResetScaleBtn.getAttribute("data-gid");
        if (gid) {
          const cfgs = getGroupConfigs();
          if (cfgs[gid]) {
            delete cfgs[gid].font_scale;
            currentConfig.group_configs = cfgs;
          }
          renderSelectedGroupsFontList();
          triggerAutoSave();
          showToast(`已重置群 ${gid} 字体大小至跟随全局`);
        }
        return;
      }

      // 0.4 精选字体卸载/删除
      const curDel = e.target.closest(".curated-delete-btn, [data-cid]");
      if (curDel) {
        e.preventDefault();
        e.stopPropagation();
        const cid = curDel.getAttribute("data-cid");
        if (cid && confirm(`确定删除该精选字体文件吗？`)) {
          (async () => {
            try {
              showToast("正在删除字体包…");
              const res = await api.post("fonts/curated_delete", { id: cid });
              if (res && res.ok) {
                showToast("🗑️ 已删除字体包");
              } else {
                showToast("删除失败: " + ((res && res.error) || "未知"));
              }
              await fetchFontStatus(true);
              await fetchFontFiles();
              await fetchCuratedFonts();
            } catch(err) {
              showToast("删除异常: " + err.message);
            }
          })();
        }
        return;
      }

      // 0.41 Emoji 删除
      const emojiDel = e.target.closest(".emoji-del-btn, [data-emoji-del]");
      if (emojiDel) {
        e.preventDefault();
        e.stopPropagation();
        deleteEmojiPack(emojiDel.getAttribute("data-emoji-del"));
        return;
      }

      // 0.42 Emoji 下载
      const emojiDl = e.target.closest(".emoji-dl-btn, [data-emoji-dl]");
      if (emojiDl) {
        e.preventDefault();
        e.stopPropagation();
        downloadEmojiPack(emojiDl.getAttribute("data-emoji-dl"));
        return;
      }

      // 0.43 独立字体文件删除
      const fontDel = e.target.closest(".font-del-btn, [data-fname]");
      if (fontDel && fontDel.getAttribute("data-fname")) {
        e.preventDefault();
        e.stopPropagation();
        deleteFont(fontDel.getAttribute("data-fname"));
        return;
      }

      // 0.5 清空全部 Emoji 缓存
      if (e.target.closest("#clearAllEmojiBtn")) {
        e.preventDefault();
        if (!confirm("确定清空全部 Emoji 资源与下载缓存吗？")) return;
        (async () => {
          try {
            showToast("正在清空全部 Emoji 缓存…");
            const res = await api.post("emoji/delete", { style: "all" });
            if (res && res.ok) {
              showToast("🗑️ 已清空全部 Emoji 缓存");
              setSegmentedValue("segEmojiStyle", "none");
              currentConfig.emoji_style = "none";
              triggerAutoSave();
            } else {
              showToast("清空提示: " + ((res && res.error) || "未知"));
            }
            await fetchEmojiPacks();
            await fetchFontStatus(true);
          } catch(err) {
            showToast("清空异常: " + err.message);
          }
        })();
        return;
      }

      // 0.6 新增自定义词库
      if (e.target.closest("#addNewPresetBtn")) {
        e.preventDefault();
        const name = prompt("请输入新词库方案名称：", "自定义敏感词库");
        if (name && name.trim()) {
          const presets = getKeywordPresets();
          const pid = "custom_" + Date.now();
          presets[pid] = { name: name.trim(), keywords: "" };
          currentConfig.keyword_presets = presets;
          currentConfig.active_keyword_preset = pid;
          populateKeywordPresets();
          renderSelectedGroupsFontList();
          triggerAutoSave();
          showToast(`✅ 已新建词库方案：${name}`);
        }
        return;
      }

      // 0.7 删除自定义词库
      if (e.target.closest("#delPresetBtn")) {
        e.preventDefault();
        const sel = document.getElementById("keywordPresetSelect");
        const cur = sel ? sel.value : "default";
        if (cur === "default") {
          showToast("⚠️ 默认标准词库不可删除");
          return;
        }
        if (confirm("确定删除当前自定义词库方案吗？")) {
          const presets = getKeywordPresets();
          delete presets[cur];
          currentConfig.keyword_presets = presets;
          currentConfig.active_keyword_preset = "default";
          populateKeywordPresets();
          renderSelectedGroupsFontList();
          triggerAutoSave();
          showToast("🗑️ 已删除该词库方案");
        }
        return;
      }

      // 1. 群聊勾选胶囊点击
      const groupChip = e.target.closest(".group-select-chip");
      if (groupChip) {
        e.preventDefault();
        const gid = groupChip.getAttribute("data-gid");
        if (gid) toggleGroupSelection(gid);
        return;
      }

      // 2. 分段选择器点击
      const segItem = e.target.closest(".seg-item");
      if (segItem) {
        e.preventDefault();
        const parent = segItem.parentElement;
        if (parent) {
          parent.querySelectorAll(".seg-item").forEach((item) => item.classList.remove("active"));
          segItem.classList.add("active");
          const val = segItem.getAttribute("data-val");

          // 风格切换更新统计卡
          if (parent.id === "segStyle") {
            const curStyleEl = document.getElementById("statCurrentStyle");
            if (curStyleEl) curStyleEl.textContent = (val || "ios").toUpperCase();
          }

          // 生效模式切换动态更新底部提示
          if (parent.id === "segGroupMode") {
            const curList = document.getElementById("cfgGroupList")?.value || "";
            updateGroupModeStatusUI(val, curList);
          }

          // 字体来源切换显隐自定义输入区
          if (parent.id === "segFontSource") {
            const customBox = document.getElementById("customFontBox");
            if (customBox) customBox.style.display = val === "custom" ? "block" : "none";
          }
          if (parent.id === "segEmojiStyle") {
            if (val !== "none") {
              const row = document.querySelector(`#emojiPacksBox [data-emoji-del="${val}"]`);
              if (!row) {
                showToast(`⚠️「${val}」尚未下载，请先点击下方列表中的「下载」按钮`);
                const prev = currentConfig.emoji_style || "none";
                parent.querySelectorAll(".seg-item").forEach(item => {
                  if (item.getAttribute("data-val") === prev) item.classList.add("active");
                  else item.classList.remove("active");
                });
                return;
              }
            }
          }
          if (parent.id === "segAiProviderMode") {
            const astrBox=document.getElementById("aiAstrbotBox");
            const customBox2=document.getElementById("aiCustomBox");
            if(astrBox) astrBox.style.display = val==="astrbot" ? "block" : "none";
            if(customBox2) customBox2.style.display = val==="custom" ? "block" : "none";
            if(val==="astrbot") fetchAiProviders();
          }

          triggerAutoSave(); // 选项切换即时自动保存生效
        }
        return;
      }

      // 3. 预设选项快捷胶囊点击（仅载入文本，不自动生成）
      const chip = e.target.closest(".m3-chip");
      if (chip) {
        e.preventDefault();
        const tplKey = chip.getAttribute("data-tpl");
        if (tplKey) applyPreset(tplKey);
        return;
      }

      // 5. 刷新按钮
      if (e.target.closest("#refreshBtn")) {
        e.preventDefault();
        refreshData();
        return;
      }

      // 6. 主题切换按钮
      if (e.target.closest("#themeToggleBtn")) {
        e.preventDefault();
        toggleTheme();
        return;
      }

      // 7. 生成预览按钮
      if (e.target.closest("#renderPreviewBtn")) {
        e.preventDefault();
        triggerPreview();
        return;
      }

      // 8. 拉取所有群聊按钮
      if (e.target.closest("#fetchGroupsBtn")) {
        e.preventDefault();
        fetchGroups();
        return;
      }

      // 9. 字体下载按钮
      if (e.target.closest("#fetchFontsBtn")) {
        e.preventDefault();
        downloadFonts();
        return;
      }

      // 11. 精选字体安装按钮
      const curatedBtn = e.target.closest(".curated-install-btn");
      if (curatedBtn && curatedBtn.hasAttribute("data-id")) {
        e.preventDefault();
        e.stopPropagation();
        installCuratedFont(curatedBtn.getAttribute("data-id"));
        return;
      }

      // 12. 应用选中持久化字体
      if (e.target.closest("#applyFontBtn")) {
        e.preventDefault();
        e.stopPropagation();
        applySelectedFont();
        return;
      }

      // 13. 自定义直链下载
      if (e.target.closest("#downloadCustomUrlBtn")) {
        e.preventDefault();
        downloadCustomUrl();
        return;
      }

      // 14. 字体大小重置
      if (e.target.closest("#fontScaleResetBtn")) {
        e.preventDefault();
        const el=document.getElementById("cfgFontScale");
        const valEl=document.getElementById("fontScaleVal");
        if(el){ el.value=100; if(valEl) valEl.textContent="100%"; triggerAutoSave(); triggerPreview(); }
        return;
      }

      // 15. 首次引导
      if (e.target.closest("#guideConfirmBtn")) { e.preventDefault(); guideConfirm(); return; }
      if (e.target.closest("#guideSkipBtn") || e.target.closest("#firstGuideModal .guide-modal-backdrop")) { e.preventDefault(); hideGuide(); return; }
    });

    // 开关状态联动
    document.addEventListener("change", function (e) {
      if (e.target && e.target.id === "cfgStarBg") {
        const starDensityGroup = document.getElementById("starDensityGroup");
        if (starDensityGroup) {
          starDensityGroup.style.display = e.target.checked ? "block" : "none";
        }
        triggerAutoSave();
      }
      if (e.target && e.target.id === "cfgEnableAiModeration") {
        const wrap = document.getElementById("aiConfigWrap");
        if (wrap) {
          wrap.style.opacity = e.target.checked ? "1" : "0.5";
          wrap.style.pointerEvents = e.target.checked ? "auto" : "none";
        }
        currentConfig.enable_ai_moderation = e.target.checked;
        if (e.target.checked && currentConfig.moderation_mode === "none") {
          currentConfig.moderation_mode = "both";
          setSegmentedValue("segModerationMode", "both");
        }
        triggerAutoSave();
      }
      if (e.target && e.target.id === "cfgEnable") {
        const statusBadge = document.getElementById("statStatusBadge");
        if (statusBadge) {
          statusBadge.textContent = e.target.checked ? "运行中" : "已暂停";
          statusBadge.className = `widget-badge ${e.target.checked ? "pill-green" : "pill-amber"}`;
        }
        triggerAutoSave();
      }
      if (e.target && e.target.id === "keywordPresetSelect") {
        const presets = getKeywordPresets();
        const activeKey = e.target.value;
        currentConfig.active_keyword_preset = activeKey;
        const txt = document.getElementById("cfgKeywords");
        if (txt && presets[activeKey]) {
          txt.value = (presets[activeKey].keywords || "").trim();
        }
        triggerAutoSave();
      }
      if (e.target && e.target.id === "fontSelectDropdown") {
        // 持久化目录字体下拉选择即时生效
        applySelectedFont();
      }
      if (e.target && (e.target.classList.contains("sg-style") || e.target.classList.contains("sg-theme") || e.target.classList.contains("sg-font") || e.target.classList.contains("sg-kw"))) {
        // 群专属风格/主题/字体/词库即时保存
        triggerAutoSave();
      }
      if (e.target && (e.target.name === "linkMode" || e.target.name === "violationAction")) {
        if (e.target.name === "violationAction") {
          const halfPosGroup = document.getElementById("mosaicHalfPosGroup");
          if (halfPosGroup) halfPosGroup.style.display = e.target.value === "mosaic_half" ? "block" : "none";
        }
        triggerAutoSave();
      }
    });

    // 输入类即时保存
    document.addEventListener("input", function (e) {
      if (e.target && e.target.id === "cfgGroupList") {
        updateGroupChipsSelection();
        renderSelectedGroupsFontList();
        updateGroupModeStatusUI(getSegmentedValue("segGroupMode", "whitelist"), e.target.value);
        triggerAutoSave();
      }
      if (e.target && e.target.classList.contains("sg-slider")) {
        const gid = e.target.getAttribute("data-gid");
        const val = parseInt(e.target.value, 10) || 100;
        const numEl = document.querySelector(`.sg-num[data-gid="${gid}"]`);
        if (numEl) numEl.value = val;
        const valEl = document.querySelector(`.sg-val[data-gid="${gid}"]`);
        if (valEl) valEl.textContent = val + "%";
        const cfgs = getGroupConfigs();
        if (!cfgs[gid]) cfgs[gid] = {};
        cfgs[gid].font_scale = val;
        currentConfig.group_configs = cfgs;
        triggerAutoSave();
      }
      if (e.target && e.target.classList.contains("sg-num")) {
        const gid = e.target.getAttribute("data-gid");
        let val = parseInt(e.target.value, 10) || 100;
        val = Math.max(50, Math.min(500, val));
        const slider = document.querySelector(`.sg-slider[data-gid="${gid}"]`);
        if (slider) slider.value = val;
        const valEl = document.querySelector(`.sg-val[data-gid="${gid}"]`);
        if (valEl) valEl.textContent = val + "%";
        const cfgs = getGroupConfigs();
        if (!cfgs[gid]) cfgs[gid] = {};
        cfgs[gid].font_scale = val;
        currentConfig.group_configs = cfgs;
        triggerAutoSave();
      }
      if (e.target && e.target.id === "cfgKeywords") {
        const sel = document.getElementById("keywordPresetSelect");
        const activeKey = sel ? sel.value : (currentConfig.active_keyword_preset || "default");
        const presets = getKeywordPresets();
        if (presets[activeKey]) {
          presets[activeKey].keywords = e.target.value.trim();
          currentConfig.keyword_presets = presets;
        }
        currentConfig.custom_keywords = e.target.value.trim();
        triggerAutoSave();
      }
      if (e.target && (e.target.id === "cfgKeywords" || e.target.id === "cfgMinLength")) {
        triggerAutoSave();
      }
      if (e.target && (e.target.id === "cfgCustomFont" || e.target.id === "cfgCustomBoldFont")) {
        triggerAutoSave();
      }
      if (e.target && e.target.id === "cfgFontScale") {
        const v=document.getElementById("fontScaleVal");
        if(v) v.textContent=e.target.value+"%";
        triggerAutoSave();
        // 字体大小实时预览
        if (window._fontScalePreviewTimer) clearTimeout(window._fontScalePreviewTimer);
        window._fontScalePreviewTimer=setTimeout(()=>triggerPreview(), 600);
      }
    });
  }

  // ---- 导出挂载到 window 方便调试 ----
  window.msg2imgApp = {
    toggleTheme: toggleTheme,
    refreshData: refreshData,
    saveConfig: saveConfig,
    triggerPreview: triggerPreview,
    applyPreset: applyPreset,
    fetchGroups: fetchGroups,
    fetchFontStatus: fetchFontStatus,
    downloadFonts: downloadFonts,
    fetchFontFiles: fetchFontFiles,
    deleteFont: deleteFont,
    fetchCuratedFonts: fetchCuratedFonts,
    installCuratedFont: installCuratedFont,
    applySelectedFont: applySelectedFont,
    downloadCustomUrl: downloadCustomUrl,
  };

  // ---- 页面初始化 ----
  function startApp() {
    initTheme();
    bindGlobalDelegation();
    notifyReady();

    const inputEl = document.getElementById("previewInput");
    if (inputEl && !inputEl.value) {
      inputEl.value = PRESET_TEMPLATES.normal;
    }

    loadData();
    fetchFontStatus(true);
    fetchFontFiles();
    fetchCuratedFonts();
    fetchEmojiPacks();
    fetchAiProviders();

    // 页面初次加载时，自动触发一次极速预览，让用户进页面立刻能看到效果
    setTimeout(() => {
      triggerPreview();
      renderSelectedGroupsFontList();
    }, 400);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startApp);
  } else {
    startApp();
  }
})();
