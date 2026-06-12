// ===== HTML 转义（防 XSS）=====
function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
const h = escapeHtml; // 简写别名
function klineUrl(code) {
    if (!code) return "javascript:;";
    var mkt = code.startsWith("6") ? "sh" : code.startsWith("0") || code.startsWith("3") ? "sz" : "bj";
    return "https://quote.eastmoney.com/concept/" + mkt + code + ".html";
}

// ===== CSRF-safe fetch 包装 =====
async function apiFetch(url, opts = {}) {
    const headers = opts.headers || {};
    if (opts.method && opts.method !== 'GET') {
        headers['X-CSRF-Protection'] = '1';
    }
    return fetch(url, {...opts, headers, credentials: 'include'});
}

// ===== 状态变量 =====
let marketStateInterval = null;

// ===== 页面初始化：hash 路由 + 默认加载持仓 =====
(function(){
    var hash = window.location.hash.slice(1) || sessionStorage.getItem("landing_hash");
    sessionStorage.removeItem("landing_hash");
    if (hash) {
        showPanel(hash);
    } else {
        loadRecommend(); showPanel("recommend"); // 默认加载ML推荐
    }
})();

(async function initMarketState(){
    try {
        const r = await fetch('/api/market/premarket', {credentials: 'include'});
        const d = await r.json();
        if (d.data && d.data.indices) {
            const sh = d.data.indices['上证指数'] || {};
            const el = document.getElementById('marketState');
            if (el) {
                const pct = sh.涨跌幅 || 0;
                el.textContent = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
                el.style.color = pct >= 0 ? '#ef5350' : '#66bb6a';
            }
            const adv = document.getElementById('marketAdvice');
            if (adv) adv.textContent = `${d.data.analysis.status} | 仓位建议: ${d.data.analysis.position_ratio}%`;
        }
    } catch(e) {}
})();

// ===== 获取当前用户并显示（可点击修改密码）=====
(async function(){
    try {
        const r = await fetch('/api/auth/me', {credentials: 'include'});
        const d = await r.json();
        if (d && d.user) {
            const el = document.getElementById('userDisplay');
            if (el) {
                el.textContent = ' ' + d.user;
                el.style.display = 'inline';
            }
        }
    } catch(e) {}
})();

// ===== 定时器管理 =====
let _activeIntervals = [];
function _clearIntervals() {
    _activeIntervals.forEach(id => clearInterval(id));
    _activeIntervals = [];
}
function _setManagedInterval(fn, ms) {
    const id = setInterval(fn, ms);
    _activeIntervals.push(id);
    return id;
}

// 修改密码
function showChangePassword() {
    document.getElementById('changePwdModal').classList.add('active');
    document.getElementById('oldPassword').value = '';
    document.getElementById('newPassword').value = '';
    document.getElementById('confirmNewPassword').value = '';
    document.getElementById('pwdMessage').style.display = 'none';
}
function closeChangePassword() {
    document.getElementById('changePwdModal').classList.remove('active');
}
async function doChangePassword() {
    const oldPwd = document.getElementById('oldPassword').value;
    const newPwd = document.getElementById('newPassword').value;
    const confirmPwd = document.getElementById('confirmNewPassword').value;
    const msgEl = document.getElementById('pwdMessage');
    if (!oldPwd || !newPwd || !confirmPwd) {
        msgEl.className = 'message error'; msgEl.textContent = '请填写所有字段'; msgEl.style.display = 'block';
        return;
    }
    if (newPwd !== confirmPwd) {
        msgEl.className = 'message error'; msgEl.textContent = '两次输入的新密码不一致'; msgEl.style.display = 'block';
        return;
    }
    if (newPwd.length < 6) {
        msgEl.className = 'message error'; msgEl.textContent = '新密码至少6位'; msgEl.style.display = 'block';
        return;
    }
    try {
        const res = await apiFetch('/api/auth/change-password', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({old_password: oldPwd, new_password: newPwd}),
            credentials: 'include'
        });
        const data = await res.json();
        if (data.error) {
            msgEl.className = 'message error'; msgEl.textContent = data.error; msgEl.style.display = 'block';
        } else {
            msgEl.className = 'message success'; msgEl.textContent = '密码修改成功！请用新密码重新登录'; msgEl.style.display = 'block';
            setTimeout(() => { closeChangePassword(); doLogout(); }, 2000);
        }
    } catch (err) {
        msgEl.className = 'message error'; msgEl.textContent = '网络错误，请重试'; msgEl.style.display = 'block';
    }
}

// 面板切换

function showPanel(name, evt) {
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav button').forEach(b => b.classList.remove('active'));
    const panel = document.getElementById('panel-' + name);
    if (!panel) return;
    panel.classList.add('active');
    if (evt && evt.target) {
        evt.target.classList.add('active');
    } else {
        document.querySelectorAll('.nav button').forEach(b => {
            if (b.getAttribute('onclick') && b.getAttribute('onclick').includes(`'${name}'`)) {
                b.classList.add('active');
            }
        });
    }
    _clearIntervals();
    if (name === 'recommend') loadRecommend();
    if (name === 'track') loadTrackStats();
    if (name === 'ai_sim') loadAiSimPerformance();
    if (name === 'ml_scan') doMLScan();
    if (name === 'scan') name = 'ml_scan';
    if (name === 'signals') loadSignals();
    if (name === 'backtest') loadBacktestSummary();
}


function startMarketStateAutoRefresh() {
    if (marketStateInterval) return; // already started
    function inTradingHours() {
        const h = new Date().getHours();
        return h >= 8 && h <= 16;
    }
    function tick() {
        if (inTradingHours()) {
            refreshMarketStateBar();
        }
    }
    tick();
    marketStateInterval = setInterval(tick, 30000);
}

async function loadRecommend(force = false) {
    const resultEl = document.getElementById('recommendResult');
    if (!resultEl) return;
    resultEl.innerHTML = '<div class="loading"><span class="spinner"></span>正在获取 ML 选股推荐...</div>';
    
    // 大盘状态
    try {
        const preRes = await fetch('/api/market/premarket', {credentials: 'include'});
        const preData = await preRes.json();
        if (preData.data && preData.data.indices) {
            const sh = preData.data.indices['上证指数'] || {};
            const pct = sh.涨跌幅 || 0;
            const a = preData.data.analysis;
            const mktEl = document.getElementById('premarketStatus');
            if (mktEl) mktEl.innerHTML =
                `上证 <b style="color:${pct>=0?'#ef5350':'#66bb6a'}">${pct>=0?'+':''}${pct.toFixed(2)}%</b> | ` +
                `${a.status} · 仓位建议 <b>${a.position_ratio}%</b>`;
        }
    } catch(e) {}

    try {
        const url = '/api/recommend/v11' + (force ? '?force_refresh=true' : '');
        const res = await fetch(url, {credentials: 'include'});
        const data = await res.json();
        
        const recs = data['推荐股票'] || [];
        if (!recs.length) {
            resultEl.innerHTML = '<div class="empty">暂无推荐</div>';
            return;
        }

        let html = '<div style="display:flex; flex-direction:column; gap:14px;">';
        recs.forEach((s, i) => {
            // v11 API: normalize field names
            s.现价 = s.现价 || 0;
            s.止损价 = s.止损价 || (s.现价 * 0.93).toFixed(2);
            s.涨跌幅 = s.涨跌幅 || '0%';
            s.代码 = s.代码 || '';
            s.名称 = s.名称 || '?';
            s.ML得分 = s.ML得分 || '--';
            s.行业 = s.行业 || '--';

            const price = s.现价 || 0;
            const stopLoss = s.止损价 || (price * 0.93).toFixed(2);
            const chg = s.涨跌幅 || '0%';
            const chgColor = chg.includes('+') ? '#ef5350' : chg.includes('-') ? '#66bb6a' : '#8892b0';
            const rankNum = i + 1;
            const mlScore = s.ML得分 || '--';
            const strength = s.排序强度 != null ? s.排序强度 : '--';
            
            html += '<div style="background:#ffffff; border-radius:10px; padding:16px; border:1px solid #e0e0e0; box-shadow:0 1px 4px rgba(0,0,0,0.06);">';
            html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px;">';
            html += `<div><span class="tag" style="background:#7c4dff; color:#fff;">#${rankNum}</span> <a href="${klineUrl(s.代码||'')}" target="_blank" style="color:#1a2332;text-decoration:none;font-size:16px;font-weight:600;">${h(s.名称||'?')} ↗</a>`;
            html += ` <a href="${klineUrl(s.代码||'')}" target="_blank" style="color:#1976d2;font-size:12px;text-decoration:none;">${h(s.代码||'')} ↗</a></div>`;
            html += `<span style="color:${chgColor}; font-size:16px; font-weight:bold;">${h(chg)}</span>`;
            html += '</div>';
            
            html += '<div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; font-size:13px;">';
            html += `<div><span style="color:#8892b0;">现价</span><br><b>${price.toFixed(2)}</b></div>`;
            html += `<div><span style="color:#8892b0;">ML得分</span><br><b style="color:${parseFloat(mlScore)<0?'#ffca28':'#ef5350'};">${mlScore}</b></div>`;
            html += `<div><span style="color:#8892b0;">排序强度</span><br><b>${strength}</b></div>`;
            html += `<div><span style="color:#8892b0;">行业</span><br><b>${h(s.行业||'--')}</b></div>`;
            html += `<div><span style="color:#8892b0;">止损价 (-7%)</span><br><b style="color:#ef5350;">${stopLoss}</b></div>`;
            html += `<div><span style="color:#8892b0;">持仓建议</span><br><b style="color:${s['持仓建议']=='2日短持'?'#ffca28':'#66bb6a'};">${h(s['持仓建议']||'5日持有')}</b></div>`;
            html += `<div><span style="color:#8892b0;">风险提示</span><br><span style="color:${(s['风险提示']||'').includes('⚠️')?'#ffca28':'#66bb6a'};font-size:11px;">${h(s['风险提示']||'--')}</span></div>`;
            html += '</div>';
            html += '</div>';
        });
        html += '</div>';
        
        // 风险提示
        html += '<div style="margin-top:14px; padding:10px; background:#fff8e1; border-radius:6px; font-size:12px; color:#666666; line-height:1.7;">';
        html += '⚠️ 风险提示：以上推荐基于V11.0纯ML模型，仅供参考。持仓建议: 2日短持(高位)/5日持有(低位)，止损-7%，等权配置。实际成交价（次日开盘）可能与信号价格存在差异。</div>';
        
        resultEl.innerHTML = html;
    } catch(e) {
        console.error(e);
        resultEl.innerHTML = '<div class="error">获取推荐失败: ' + h(e.message) + '</div>';
    }
}

// 更新时间
function updateTime() {
    const now = new Date();
    document.getElementById('nowTime').textContent = now.toLocaleString('zh-CN');
}
setInterval(updateTime, 1000);
updateTime();

// getLocalIP removed - localIp element was commented out

// 加载持仓数据
async function loadPositions() {
    try {
        const res = await fetch('/api/positions', {credentials: 'include'});
        if (!res.ok) {
            throw new Error(`HTTP ${res.status}: ${await res.text()}`);
        }
        const data = await res.json();

        if (data.error) {
            document.getElementById('positionsTable').innerHTML = `<tr><td colspan="10" class="error">${h(data.error)}</td></tr>`;
            return;
        }

        // 显示实时数据异常提示（东财API不可用时）
        if (data._note) {
            showToast(data._note, 'warning', 8000);
        }

        // 更新汇总
        const 汇总 = data.汇总;
        const profitPct = 汇总.总成本 > 0 ? (汇总.总盈亏 / 汇总.总成本 * 100).toFixed(2) : '0.00';
        document.getElementById('总成本').textContent = 汇总.总成本.toFixed(2) + ' 元';
        document.getElementById('总市值').textContent = 汇总.总市值.toFixed(2) + ' 元';
        const 盈亏El = document.getElementById('总盈亏');
        盈亏El.textContent = 汇总.总盈亏.toFixed(2) + ' 元';
        盈亏El.className = 'value ' + (汇总.总盈亏 >= 0 ? 'profit' : 'loss');
        const 比例El = document.getElementById('盈亏比例');
        比例El.textContent = (汇总.总盈亏 >= 0 ? '+' : '') + profitPct + '%';
        比例El.className = 'value ' + (汇总.总盈亏 >= 0 ? 'profit' : 'loss');

        // 更新表格（暂时统一用表格格式，移动端也能水平滚动）
        const positions = data.持仓 || [];
        const tbody = document.getElementById('positionsTable');
        tbody.innerHTML = positions.map(s => {
            const isProfit = s.浮动盈亏 >= 0;
            const signalTag = getSignalTag(s.信号);
            const ecCode = s.代码.slice(2).toLowerCase();
            const ecMarket = s.代码.slice(0, 2).toLowerCase();
            const ecUrl = `https://quote.eastmoney.com/concept/${ecMarket}${ecCode}.html`;
            return `<tr>
                <td><a href="${ecUrl}" target="_blank" style="color:#64b5f6;text-decoration:none"><strong>${s.名称}</strong> ↗</a></td>
                <td><a href="${ecUrl}" target="_blank" style="color:#64b5f6;text-decoration:none">${s.代码}</a></td>
                <td>${s.数量}</td>
                <td>${s.成本}元</td>
                <td>${s.现价}元</td>
                <td style="color: ${isProfit ? '#ef5350' : '#66bb6a'}">${isProfit ? '+' : ''}${s.浮动盈亏.toFixed(2)}元</td>
                <td style="color: ${isProfit ? '#ef5350' : '#66bb6a'}">${isProfit ? '+' : ''}${s.盈亏比例.toFixed(2)}%</td>
                <td>${s.止损价}元</td>
                <td style=\"color: #ffa726;\">${s.atr_stop_loss ? s.atr_stop_loss + '元 (2×ATR:' + s.atr_val + ')' : '-'}</td>
                <td>${s.止盈价}元</td>
                <td>${signalTag}</td>
            </tr>`;
        }).join('');

        // 加载市场情绪
        loadSentiment();
        // 加载市场状态
        loadMarketState();

        // 记录加载状态
        // debug: 持仓加载完成
    } catch (e) {
        console.error('持仓加载失败:', e);
        document.getElementById('positionsTable').innerHTML = '<tr><td colspan="10" class="error">数据加载失败: ' + h(e.message) + '</td></tr>';
    }
}

// 加载市场情绪
async function loadSentiment() {
    try {
        const res = await fetch('/api/sentiment', {credentials: 'include'});
        const d = await res.json();
        if (d.error) {
            document.getElementById('市场情绪').textContent = '--';
            return;
        }
        const sentimentColor = d.市场情绪.includes('乐观') ? '#ef5350' : d.市场情绪.includes('悲观') ? '#66bb6a' : '#ffca28';
        document.getElementById('市场情绪').textContent = d.市场情绪;
        document.getElementById('市场情绪').style.color = sentimentColor;
        document.getElementById('涨跌比').textContent = `涨跌比: ${d.涨跌比} (↑${d.上涨家数} / ↓${d.下跌家数})`;
        document.getElementById('涨停跌停').textContent = `涨停 ${d.涨停家数} | 跌停 ${d.跌停家数}`;
    } catch (e) {
        document.getElementById('市场情绪').textContent = '--';
    }
}

// 加载市场状态（优先 premarket 公开接口，无需登录）
async function loadMarketState() {
    try {
        // 先尝试走 premarket（公开接口）
        const pre = await fetch('/api/market/premarket', {credentials: 'include'});
        const pd = await pre.json();
        const pdata = pd.data;
        if (pdata && pdata.indices) {
            const indices = pdata.indices;
            const sh = indices['上证指数'];
            const changePct = sh ? sh.涨跌幅 : 0;
            const stateEl = document.getElementById('marketState');
            stateEl.textContent = `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%`;
            stateEl.style.color = changePct >= 0 ? '#ef5350' : '#66bb6a';
            const avg = Object.values(indices).reduce((s, v) => s + (v.涨跌幅 || 0), 0) / Math.max(Object.keys(indices).length, 1);
            document.getElementById('marketAdvice').textContent =
                `${pdata.analysis.status} | 仓位建议: ${pdata.analysis.position_ratio}%`;
            return;
        }
    } catch(e) { /* premarket 失败，尝试登录接口 */ }

    // 备选：走登录接口
    try {
        const res = await fetch('/api/market/state', {credentials: 'include'});
        const d = await res.json();
        if (d.error) {
            document.getElementById('marketState').textContent = '--';
            return;
        }
        const stateEl = document.getElementById('marketState');
        const mktChg = d.mkt_chg || 0;
        stateEl.textContent = `${mktChg >= 0 ? '+' : ''}${mktChg.toFixed(2)}%`;
        stateEl.style.color = mktChg >= 0 ? '#ef5350' : '#66bb6a';
        document.getElementById('marketAdvice').textContent = `涨跌比: ${d.breadth_ratio || 50}%`;
    } catch (e) {
        document.getElementById('marketState').textContent = '--';
    }
}

function getSignalTag(signal) {
    const map = {
        '止盈': '<span class="tag tag-red">止盈</span>',
        '止损': '<span class="tag tag-green">止损</span>',
        '关注': '<span class="tag tag-yellow">关注</span>',
        '持有': '<span class="tag tag-blue">持有</span>',
        '数据异常': '<span class="tag">--</span>',
    };
    return map[signal] || `<span class="tag">${signal}</span>`;
}

// 个股分析
async function doAnalyze() {
    const code = document.getElementById('stockCode').value.trim();
    let market = document.getElementById('marketSelect').value;
    if (market === 'auto') market = (code.startsWith('6') ? 'sh' : 'sz');
    if (!code) return;

    const resultEl = document.getElementById('analysisResult');
    resultEl.innerHTML = '<div class="loading"><span class="spinner"></span>分析中，请稍候...</div>';

    try {
        const res = await fetch(`/api/analysis/${market}/${code}`, {credentials: 'include'});
        const data = await res.json();

        if (data.error) {
            resultEl.innerHTML = `<div class="error">${h(data.error)}</div>`;
            return;
        }

        const basic = data['一、基础数据'];
        const tech = data['二、技术面'];
        const score = data['五、综合评分'];
        if (!score) {
            resultEl.innerHTML = `<div class="error">分析数据不完整${data.error ? '：' + h(data.error) : ''}</div>`;
            return;
        }
        const totalScore = parseFloat(score.总分);
        const scoreClass = totalScore >= 75 ? 'high' : totalScore >= 60 ? 'mid' : 'low';
        const sugClass = score.操作建议.includes('买入') ? 'buy' : score.操作建议.includes('持有') ? 'hold' : 'watch';

        // ML模型数据
        const aiModel = data['六、AI模型'];
        const summary = data['七、总结'];

        // 涨跌幅颜色
        const pctVal = parseFloat(basic.涨跌幅);
        const pctColor = pctVal >= 0 ? '#ef5350' : '#66bb6a';
        // KDJ颜色
        let kdjColor = '#8892b0';
        if (tech.KDJ) {
            if (tech.KDJ.indexOf('超买') >= 0) kdjColor = '#ef5350';
            else if (tech.KDJ.indexOf('超卖') >= 0) kdjColor = '#66bb6a';
            else if (tech.KDJ.indexOf('金叉') >= 0) kdjColor = '#ef5350';
            else if (tech.KDJ.indexOf('正常') >= 0) kdjColor = 'var(--primary)';
        }
        // 量比颜色
        const vrVal = parseFloat(basic.量比);
        const vrColor = vrVal >= 1.5 ? '#ef5350' : vrVal >= 1.0 ? '#ffca28' : '#8892b0';
        // 换手率颜色
        const trVal = parseFloat(basic.换手率);
        const trColor = trVal >= 5 ? '#ef5350' : trVal >= 2 ? '#ffca28' : '#8892b0';
        // ML概率颜色
        let mlProbColor = '#8892b0';
        let mlProb = 0;
        if (aiModel && aiModel.ML概率) {
            mlProb = parseFloat(aiModel.ML概率.replace('%', ''));
            mlProbColor = mlProb >= 70 ? '#ef5350' : mlProb >= 50 ? '#ffca28' : '#66bb6a';
        }

        resultEl.innerHTML = `
            <div class="analysis-card">
                <div class="analysis-header">
                    <div>
                        <h3>${data.股票名称}</h3>
                        <span class="code">${data.股票代码}</span>
                    </div>
                    <div class="score-circle score-${scoreClass}">
                        ${totalScore}
                        <span class="score-label">${score.风险等级}风险</span>
                    </div>
                </div>

                <div class="analysis-grid">
                    <div class="analysis-section">
                        <h4> 基础数据</h4>
                        <ul>
                            <li><span class="li-label">现价</span><span class="li-value">${basic['现价']}</span></li>
                            <li><span class="li-label">涨跌幅</span><span class="li-value" style="color:${pctColor};font-weight:600;">${basic.涨跌幅}</span></li>
                            <li><span class="li-label">今开 / 昨收</span><span class="li-value">${basic.今开} / ${basic.昨收}</span></li>
                            <li><span class="li-label">最高 / 最低</span><span class="li-value">${basic.最高} / ${basic.最低}</span></li>
                            <li><span class="li-label">换手率</span><span class="li-value" style="color:${trColor};">${basic.换手率}</span></li>
                            <li><span class="li-label">量比</span><span class="li-value" style="color:${vrColor};">${basic.量比}</span></li>
                            <li><span class="li-label">涨停价</span><span class="li-value">${basic.涨停价}</span></li>
                            <li><span class="li-label">跌停价</span><span class="li-value">${basic.跌停价}</span></li>
                        </ul>
                    </div>
                    <div class="analysis-section">
                        <h4> 技术面</h4>
                        <ul>
                            <li><span class="li-label">均线趋势</span><span class="li-value" style="color:${tech['均线趋势']==='多头'?'#ef5350':tech['均线趋势']==='空头'?'#66bb6a':'#ffca28'};font-weight:500;">${tech['均线趋势']}</span></li>
                            <li><span class="li-label">价格位置</span><span class="li-value">${tech['价格位置']}</span></li>
                            <li><span class="li-label">RPS评分</span><span class="li-value" style="color:${tech.RPS评分?(parseInt(tech.RPS评分)>=80?'#ef5350':parseInt(tech.RPS评分)>=60?'#ffca28':'#8892b0'):'#8892b0'};">${tech.RPS评分}</span></li>
                            <li><span class="li-label">量价配合</span><span class="li-value" style="color:${tech['量价配合']==='放量'||tech['量价配合']==='放量上涨'?'#ef5350':tech['量价配合']==='放量下跌'?'#66bb6a':'#8892b0'};">${tech['量价配合']}</span></li>
                            ${tech.MACD ? `<li><span class="li-label">MACD</span><span class="li-value" style="font-size:12px;font-weight:500;color:${tech.MACD.indexOf('金叉')>=0?'#ef5350':tech.MACD.indexOf('死叉')>=0?'#66bb6a':'#8892b0'}">${tech.MACD}</span></li>` : ''}
                            ${tech.KDJ ? `<li><span class="li-label">KDJ</span><span class="li-value" style="font-size:12px;color:${kdjColor};">${tech.KDJ}</span></li>` : ''}
                            ${tech.ATR ? `<li><span class="li-label">ATR</span><span class="li-value">${tech.ATR}</span></li>` : ''}
                            ${tech['布林带'] ? `<li><span class="li-label">布林带</span><span class="li-value" style="font-size:12px;">${tech['布林带']}</span></li>` : ''}
                            ${tech['布林带位置'] ? `<li><span class="li-label">布林位置</span><span class="li-value"><div style="display:inline-flex;align-items:center;gap:6px;"><span>${tech['布林带位置']}</span><span style="display:inline-block;width:60px;height:6px;background:#e8e8e8;border-radius:3px;overflow:hidden;vertical-align:middle;"><span style="display:block;height:100%;width:${parseInt(tech['布林带位置'])||50}%;border-radius:3px;background:${parseInt(tech['布林带位置'])>=80?'#ef5350':parseInt(tech['布林带位置'])<=20?'#66bb6a':'var(--primary)'};"></span></span></div></span></li>` : ''}
                            ${tech['板块'] ? `<li><span class="li-label">所属板块</span><span class="li-value">${tech['板块']}</span></li>` : ''}
                        </ul>
                    </div>
                </div>

                <!-- 资金面 + AI模型 + 基本面 三栏横排 -->
                <div class="info-grid-3" style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:16px;">
                    <div class="analysis-section" style="background:#f9fafb;border-radius:8px;padding:12px;">
                        <h4 style="font-size:13px;color:var(--primary-dark);margin-bottom:8px;"> 资金面</h4>
                        <ul style="list-style:none;padding:0;margin:0;">
                            <li style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px;"><span style="color:#888;">主力净流入</span><span style="color:${(()=>{const v=(data['三、资金面'].主力净流入||'N/A');return v!=='N/A'&&parseFloat(v)>=0?'#ef5350':'#66bb6a';})()};">${data['三、资金面'].主力净流入 || 'N/A'}</span></li>
                            <li style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px;"><span style="color:#888;">主力净流入占比</span><span>${data['三、资金面']['主力净流入占比'] || 'N/A'}</span></li>
                        </ul>
                    </div>
                    ${aiModel ? `
                    <div class="analysis-section" style="background:#f9fafb;border-radius:8px;padding:12px;">
                        <h4 style="font-size:13px;color:#7c4dff;margin-bottom:8px;"> AI模型</h4>
                        <ul style="list-style:none;padding:0;margin:0;">
                            <li style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px;"><span style="color:#888;">ML概率</span><span style="color:${mlProbColor};font-weight:bold;">${aiModel.ML概率 || 'N/A'}</span></li>
                            <li style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px;"><span style="color:#888;">排序分</span><span style="color:${(()=>{const v=aiModel.排序分||'N/A';if(v==='N/A')return '#8892b0';const n=parseFloat(v);return n>=1?'#ef5350':n>=0?'#ffca28':'#66bb6a';})()};">${aiModel.排序分 || 'N/A'}</span></li>
                            <li style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px;"><span style="color:#888;">资金趋势</span><span style="color:${aiModel.资金趋势==='accelerating'||aiModel.资金趋势==='流入'?'#ef5350':aiModel.资金趋势==='流出'?'#66bb6a':'#8892b0'};">${{accelerating:'加速流入',steady:'平稳',weakening:'减弱',unknown:'未知'}[aiModel.资金趋势] || aiModel.资金趋势 || 'N/A'}</span></li>
                            <li style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px;"><span style="color:#888;">模型</span><span>${aiModel.模型名称 || 'N/A'}</span></li>
                        </ul>
                    </div>` : `<div></div>`}
                    <div class="analysis-section" style="background:#f9fafb;border-radius:8px;padding:12px;">
                        <h4 style="font-size:13px;color:#e65100;margin-bottom:8px;"> 基本面</h4>
                        <ul style="list-style:none;padding:0;margin:0;">
                            <li style="display:flex;justify-content:space-between;padding:3px 0;font-size:12px;"><span style="color:#888;">行业</span><span>${data['四、基本面'].行业 || 'N/A'}</span></li>
                            <li style="display:flex;justify-content:space-between;padding:3px 0;font-size:12px;"><span style="color:#888;">PE</span><span>${data['四、基本面'].PE || 'N/A'}</span></li>
                            <li style="display:flex;justify-content:space-between;padding:3px 0;font-size:12px;"><span style="color:#888;">PB</span><span>${data['四、基本面'].PB || 'N/A'}</span></li>
                            <li style="display:flex;justify-content:space-between;padding:3px 0;font-size:12px;"><span style="color:#888;">毛利率</span><span>${data['四、基本面'].毛利率 || 'N/A'}</span></li>
                            <li style="display:flex;justify-content:space-between;padding:3px 0;font-size:12px;"><span style="color:#888;">净利率</span><span>${data['四、基本面'].净利率 || 'N/A'}</span></li>
                            <li style="display:flex;justify-content:space-between;padding:3px 0;font-size:12px;"><span style="color:#888;">ROE</span><span>${data['四、基本面'].ROE || 'N/A'}</span></li>
                            <li style="display:flex;justify-content:space-between;padding:3px 0;font-size:12px;"><span style="color:#888;">盈亏</span><span style="color: ${data['四、基本面'].盈亏 === '盈利' ? '#ef5350' : data['四、基本面'].盈亏 === '亏损' ? '#66bb6a' : '#8892b0'}">${data['四、基本面'].盈亏 || 'N/A'}</span></li>
                        </ul>
                    </div>
                </div>

                <div class="suggestion ${sugClass}" style="font-size:15px;padding:14px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                    <span style="font-weight:600;">${score.操作建议}</span>
                    <span style="font-size:13px;">止损 ${score.止损价} → 目标 ${score.目标价}</span>
                    <span style="font-size:13px;display:inline-flex;align-items:center;gap:6px;">
                        <span style="display:inline-block;width:50px;height:6px;background:rgba(255,255,255,0.3);border-radius:3px;overflow:hidden;vertical-align:middle;">
                            <span style="display:block;height:100%;width:${Math.min(totalScore,100)}%;background:rgba(255,255,255,0.9);border-radius:3px;"></span>
                        </span>
                        ${totalScore}分
                    </span>
                </div>

                ${summary ? `
                <div style="margin-top:16px; background:#f8f9fe; border:1px solid #e0e0e0; border-radius:10px; padding:16px;">
                    <h4 style="color:var(--primary-dark); font-size:15px; margin-bottom:12px;"> 分析总结</h4>
                    <div style="margin-bottom:10px;">
                        <div style="font-size:13px; color:var(--primary-dark); font-weight:500; margin-bottom:4px;"> 技术面综合</div>
                        <div style="font-size:13px; color:#333; line-height:1.6;">${summary['技术面综合']}</div>
                    </div>
                    <div style="margin-bottom:10px;">
                        <div style="font-size:13px; color:var(--primary-dark); font-weight:500; margin-bottom:4px;"> 资金面</div>
                        <div style="font-size:13px; color:#333; line-height:1.6;">${summary['资金面']}</div>
                    </div>
                    ${summary['模型观点'] ? `
                    <div style="margin-bottom:10px;">
                        <div style="font-size:13px; color:var(--primary-dark); font-weight:500; margin-bottom:4px;"> AI模型观点</div>
                        <div style="font-size:13px; color:#333; line-height:1.6;">${summary['模型观点']}</div>
                    </div>` : ''}
                    ${summary['波动分析'] ? `
                    <div style="margin-bottom:10px;">
                        <div style="font-size:13px; color:#7c4dff; font-weight:500; margin-bottom:4px;"> 波动分析</div>
                        <div style="font-size:13px; color:#333; line-height:1.6;">${summary['波动分析']}</div>
                    </div>` : ''}
                    ${summary['布林带位置'] ? `
                    <div style="margin-bottom:10px;">
                        <div style="font-size:13px; color:#7c4dff; font-weight:500; margin-bottom:4px;"> 布林带位置</div>
                        <div style="font-size:13px; color:#333; line-height:1.6;">${summary['布林带位置']}</div>
                    </div>` : ''}
                    ${summary['板块趋势'] ? `
                    <div style="margin-bottom:10px;">
                        <div style="font-size:13px; color:#e65100; font-weight:500; margin-bottom:4px;"> 板块趋势</div>
                        <div style="font-size:13px; color:#333; line-height:1.6;">${summary['板块趋势']}</div>
                    </div>` : ''}
                    ${summary['大盘情绪'] ? `
                    <div style="margin-bottom:10px;">
                        <div style="font-size:13px; color:#e65100; font-weight:500; margin-bottom:4px;"> 大盘情绪</div>
                        <div style="font-size:13px; color:#333; line-height:1.6;">${summary['大盘情绪']}</div>
                    </div>` : ''}
                    <div style="margin-bottom:10px;">
                        <div style="font-size:13px; color:#e65100; font-weight:500; margin-bottom:4px;">️ 风险提示</div>
                        <div style="font-size:13px; color:#333; line-height:1.6;">${Array.isArray(summary['风险提示']) ? summary['风险提示'].map(r => {
                            let bg = '#fff3e0', icon = '️';
                            if (r.includes('超买') || r.includes('追高')) { bg = '#ffebee'; icon = ''; }
                            else if (r.includes('死叉') || r.includes('流出') || r.includes('下跌')) { bg = '#fff3e0'; icon = '️'; }
                            else if (r.includes('空头') || r.includes('重仓')) { bg = '#fff8e1'; icon = ''; }
                            return '<div style="padding:4px 10px;margin:3px 0;background:'+bg+';border-radius:4px;font-size:13px;">'+icon+' '+r+'</div>';
                        }).join('') : summary['风险提示']}</div>
                    </div>
                    <div>
                        <div style="font-size:13px; color:#2e7d32; font-weight:500; margin-bottom:4px;"> 操作参考</div>
                        <div style="font-size:13px; color:#333; line-height:1.6;">${summary['操作参考']}</div>
                    </div>
                </div>` : ''}

                <div style="text-align: right; color: #8892b0; font-size: 12px; margin-top: 12px;">
                    分析时间：${data.更新时间}
                </div>
            </div>
        `;

        // 技术信号（买卖点分析）
        try {
            const tsRes = await fetch(`/api/technical_signals?code=${code}&market=${market}`, {credentials: 'include'});
            const ts = await tsRes.json();
            if (!ts.error) {
                const trendDir = ts['一、趋势判断']?.趋势方向 || '--';
                const buyScore = ts['二、买入信号']?.评分 || 0;
                const sellScore = ts['三、卖出信号']?.评分 || 0;
                const buyReasons = ts['二、买入信号']?.理由 || '';
                const stopLoss = ts['四、最佳买入区间']?.止损价 || '--';
                const stopLossPct = ts['四、最佳买入区间']?.止损幅度 || '';
                const buyRange = ts['四、最佳买入区间']?.建议区间 || '--';
                const consTarget = ts['五、止盈目标']?.['保守目标(ATR1.5x)'] || '--';
                const neutTarget = ts['五、止盈目标']?.['中性目标(ATR2.5x)'] || '--';
                const optTarget = ts['五、止盈目标']?.['乐观目标(ATR4.0x)'] || '--';
                const macd = ts['七、技术指标']?.MACD?.多空状态 || '--';
                const kdj = ts['七、技术指标']?.KDJ?.状态 || '--';
                const rsi = ts['七、技术指标']?.RSI?.RSI || '--';
                const trendColor = trendDir === '上升' ? '#ef5350' : trendDir === '下降' ? '#66bb6a' : '#ffca28';
                resultEl.innerHTML += `<div class="analysis-card" style="margin-top:16px;">
                    <h4 style="color:var(--primary-dark); margin-bottom:12px;"> 技术信号分析</h4>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;margin-bottom:12px;">
                        <div class="card"><div class="label">趋势方向</div><div class="value" style="color:${trendColor};font-size:18px;">${trendDir}</div></div>
                        <div class="card"><div class="label">买入评分</div><div class="value" style="color:#ef5350;font-size:18px;">${buyScore}/100</div></div>
                        <div class="card"><div class="label">卖出评分</div><div class="value" style="color:#66bb6a;font-size:18px;">${sellScore}/100</div></div>
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;font-size:13px;color:#666;margin-bottom:12px;">
                        <span>MACD: <strong>${macd}</strong></span>
                        <span>KDJ: <strong>${kdj}</strong></span>
                        <span>RSI: <strong>${rsi}</strong></span>
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;font-size:13px;color:#666;">
                        <span>买入区间: <strong style="color:#ef5350;">${buyRange}</strong></span>
                        <span>止损: <strong style="color:#ff9800;">${stopLoss} ${stopLossPct}</strong></span>
                        <span>保守目标: <strong style="color:#66bb6a;">${consTarget}</strong></span>
                        <span>中性目标: <strong style="color:#4caf50;">${neutTarget}</strong></span>
                        <span>乐观目标: <strong style="color:#2e7d32;">${optTarget}</strong></span>
                    </div>
                    ${buyReasons && buyReasons !== '信号不足' ? `<div style="margin-top:8px;font-size:12px;color:#8892b0;"> 买入信号: ${buyReasons}</div>` : ''}
                </div>`;
            }
        } catch(e) { /* 静默 */ }
    } catch (e) {
        resultEl.innerHTML = `<div class="error">分析失败: ${h(e.message)}</div>`;
    }
}


// Tab 切换函数 + 板块加载


// ==================== AI模拟组合 ====================
async function loadAiSimPerformance() {
    const el = document.getElementById('aiSimDashboard');
    if (!el) return;
    el.innerHTML = '<div class="loading"><span class="spinner"></span>正在加载AI模拟组合数据...</div>';
    try {
        const res = await fetch('/api/ai_sim/performance', {credentials: 'include'});
        const d = await res.json();
        if (d.error) { el.innerHTML = `<div class="error">${h(d.error)}</div>`; return; }
        const acct = d.account || {};
        const winRate = (acct.win_rate||0).toFixed(1);
        const totalPnl = acct.total_pnl || 0;
        const totalPnlPct = (acct.total_pnl_pct||0).toFixed(2);
        const tradeCount = acct.trade_count || 0;
        const totalRec = d.summary && d.summary.length ? d.summary[0] : 0;
        let html = '<div style="display:flex;gap:16px;margin-bottom:16px;flex-wrap:wrap;">';
        html += `<div class="card"><div style="color:#8892b0;font-size:11px;">总推荐</div><div style="font-size:20px;font-weight:bold;">${totalRec}次</div></div>`;
        html += `<div class="card"><div style="color:#8892b0;font-size:11px;">交易</div><div style="font-size:20px;font-weight:bold;">${tradeCount}次</div></div>`;
        html += `<div class="card"><div style="color:#8892b0;font-size:11px;">胜率</div><div style="font-size:20px;font-weight:bold;color:${winRate>=50?'#ef5350':'#66bb6a'}">${winRate}%</div></div>`;
        html += `<div class="card"><div style="color:#8892b0;font-size:11px;">总盈亏</div><div style="font-size:20px;font-weight:bold;color:${totalPnl>=0?'#ef5350':'#66bb6a'}">${totalPnl.toFixed(2)}元</div></div>`;
        html += `<div class="card"><div style="color:#8892b0;font-size:11px;">收益率</div><div style="font-size:20px;font-weight:bold;color:${totalPnl>=0?'#ef5350':'#66bb6a'}">${totalPnlPct}%</div></div>`;
        html += '</div>';

        const positions = d.positions || [];
        if (positions.length > 0) {
            html += '<h4 style="margin-top:12px;font-size:14px;color:#e0e0e0;">当前持仓</h4>';
            html += '<div class="table-wrap"><table style="font-size:12px;"><thead><tr><th>代码</th><th>名称</th><th>成本</th><th>现价</th><th>数量</th><th>市值</th><th>盈亏</th><th>盈亏率</th><th>持有天数</th></tr></thead><tbody>';
            positions.forEach(p => {
                const ppnl = p.pnl || 0;
                const pc = ppnl >= 0 ? '#ef5350' : '#66bb6a';
                const pcode = (p.ts_code||'').replace('.SZ','').replace('.SH','');
                html += `<tr><td>${pcode}</td><td>${h(p.name||'')}</td><td>${(p.buy_price||0).toFixed(2)}</td><td>${(p.current_price||0).toFixed(2)}</td><td>${p.shares||0}</td><td>${(p.market_value||0).toFixed(0)}</td><td style="color:${pc}">${ppnl.toFixed(2)}</td><td style="color:${pc}">${(p.pnl_pct||0).toFixed(2)}%</td><td>${p.days_held||0}天</td></tr>`;
            });
            html += '</tbody></table></div>';
        }

        const recs = d.recommendations || [];
        if (recs.length > 0) {
            html += '<h4 style="margin-top:12px;font-size:14px;color:#e0e0e0;">推荐历史（近30条）</h4>';
            html += '<div class="table-wrap" style="max-height:400px;overflow-y:auto;"><table style="font-size:12px;"><thead><tr><th>日期</th><th>排名</th><th>代码</th><th>名称</th><th>综合分</th><th>1日</th><th>3日</th><th>5日</th></tr></thead><tbody>';
            recs.slice(0,30).forEach(r => {
                const rc = (r.ts_code||'').replace('.SZ','').replace('.SH','');
                html += `<tr><td>${r.recommend_date||''}</td><td>#${r.rec_rank||''}</td><td>${rc}</td><td>${h(r.name||'')}</td><td>${r.total_score||0}</td>`;
                ['ret_1d','ret_3d','ret_5d'].forEach(k => {
                    const v = r[k];
                    html += `<td style="color:${v==null?'#8892b0':(v>=0?'#ef5350':'#66bb6a')}">${v!=null?(v*100).toFixed(2)+'%':'--'}</td>`;
                });
                html += '</tr>';
            });
            html += '</tbody></table></div>';
        }
        el.innerHTML = html;
    } catch(e) {
        el.innerHTML = `<div class="error">加载失败: ${h(e.message)}</div>`;
    }
}


async function runAiSimToday() {
    const el = document.getElementById('aiSimDashboard');
    el.innerHTML = getLoadingHTML('正在记录今日TOP5...');
    try {
        const res = await apiFetch('/api/ai_sim/run', {method:'POST'});
        const d = await res.json();
        if (d.error) { el.innerHTML = `<div class="error">${h(d.error)}</div>`; return; }
        await loadAiSimPerformance();
    } catch(e) {
        el.innerHTML = `<div class="error">记录失败: ${h(e.message)}</div>`;
    }
}

// ==================== 模拟交易 ====================

// ==================== 胜率追踪 ====================
async function loadTrackStats() {
    const statsEl = document.getElementById('trackStats');
    const histEl = document.getElementById('trackHistory');
    statsEl.innerHTML = getLoadingHTML('加载统计数据...');
    histEl.innerHTML = getLoadingHTML('加载历史记录...');
    try {
        // 同时获取摘要和明细
        const [statsRes, histRes] = await Promise.all([
            fetch('/api/track/stats', {credentials:'include'}),
            fetch('/api/track/history', {credentials:'include'})
        ]);
        const d = await statsRes.json();
        const h = await histRes.json();

        // 摘要统计
        const total = d.total_recommendations || 0;
        const wins = d.win_count || 0;
        const losses = d.loss_count || 0;
        const winRate = (d.win_rate || 0).toFixed(1);
        const totalPnl = (d.total_pnl || 0).toFixed(2);
        const avgProfit = (d.avg_profit || 0).toFixed(2);
        const wrColor = parseFloat(winRate) >= 50 ? '#ef5350' : '#66bb6a';
        const plColor = parseFloat(totalPnl) >= 0 ? '#ef5350' : '#66bb6a';

        if (total > 0) {
            statsEl.innerHTML = `<div style="display:flex;gap:16px;margin-bottom:12px;flex-wrap:wrap;">
                <div style="background:#ffffff;border-radius:8px;padding:10px 14px;border:1px solid #e0e0e0;"><div style="color:#8892b0;font-size:11px;">总推荐</div><div style="font-size:18px;font-weight:bold;">${total}次</div></div>
                <div style="background:#ffffff;border-radius:8px;padding:10px 14px;border:1px solid #e0e0e0;"><div style="color:#8892b0;font-size:11px;">盈利</div><div style="font-size:18px;font-weight:bold;color:#ef5350;">${wins}次</div></div>
                <div style="background:#ffffff;border-radius:8px;padding:10px 14px;border:1px solid #e0e0e0;"><div style="color:#8892b0;font-size:11px;">亏损</div><div style="font-size:18px;font-weight:bold;color:#66bb6a;">${losses}次</div></div>
                <div style="background:#ffffff;border-radius:8px;padding:10px 14px;border:1px solid #e0e0e0;"><div style="color:#8892b0;font-size:11px;">胜率</div><div style="font-size:18px;font-weight:bold;color:${wrColor};">${winRate}%</div></div>
                <div style="background:#ffffff;border-radius:8px;padding:10px 14px;border:1px solid #e0e0e0;"><div style="color:#8892b0;font-size:11px;">累计收益</div><div style="font-size:18px;font-weight:bold;color:${plColor};">${totalPnl}%</div></div>
                <div style="background:#ffffff;border-radius:8px;padding:10px 14px;border:1px solid #e0e0e0;"><div style="color:#8892b0;font-size:11px;">平均收益</div><div style="font-size:18px;font-weight:bold;color:${plColor};">${avgProfit}%</div></div>
            </div>`;
        } else {
            statsEl.innerHTML = '<div class="empty">暂无统计数据</div>';
        }

        // 明细：recommendations → stocks
        const recs = h.recommendations || [];
        if (recs.length > 0) {
            let html = '<table style="width:100%;font-size:12px;"><thead><tr><th>日期</th><th>策略</th><th>代码</th><th>名称</th><th>推荐价</th><th>评分</th><th>1日</th><th>1周</th><th>1月</th></tr></thead><tbody>';
            for (const rec of recs) {
                const date = rec.date || '';
                const strategy = rec.strategy || '';
                const stocks = rec.stocks || [];
                for (const s of stocks) {
                    const code = (s.code||'').replace('SZ','').replace('SH','');
                    const r1d = s['1day_result'];
                    const r1w = s['1week_result'];
                    const r1m = s['1month_result'];
                    html += `<tr>
                        <td>${date}</td><td style="font-size:11px;color:#8892b0;">${strategy}</td>
                        <td>${code}</td><td>${s.name||''}</td><td>${s.price||0}</td><td>${s.score||0}</td>
                        <td style="color:${r1d==null?'#8892b0':(r1d>=0?'#ef5350':'#66bb6a')}">${r1d!=null?r1d.toFixed(2)+'%':'--'}</td>
                        <td style="color:${r1w==null?'#8892b0':(r1w>=0?'#ef5350':'#66bb6a')}">${r1w!=null?r1w.toFixed(2)+'%':'--'}</td>
                        <td style="color:${r1m==null?'#8892b0':(r1m>=0?'#ef5350':'#66bb6a')}">${r1m!=null?r1m.toFixed(2)+'%':'--'}</td>
                    </tr>`;
                }
            }
            html += '</tbody></table>';
            histEl.innerHTML = html;
        } else {
            histEl.innerHTML = '<div class="empty">暂无历史记录</div>';
        }

        // 加载收益曲线（基于模拟建仓NAV快照）
        try {
            const curveRes = await fetch('/api/track/curve', {credentials:'include'});
            const curveData = await curveRes.json();
            const points = curveData.curve || [];
            if (points.length > 0) {
                const summary = curveData.summary || {};
                const pnlColor = summary.cum_ret >= 0 ? '#ef5350' : '#66bb6a';
                document.getElementById('curveSummary').innerHTML =
                    `收益: <b style="color:${pnlColor}">${summary.cum_ret.toFixed(1)}%</b> ` +
                    `回撤: <b style="color:#66bb6a">-${summary.max_drawdown.toFixed(1)}%</b> ` +
                    `交易: <b>${summary.total_trades}</b>次 ` +
                    `<span style="font-size:11px;color:#8892b0;">模拟建仓</span>`;

                // 销毁旧Chart实例避免重复创建
                const chartCanvas = document.getElementById('trackChart');
                if (window._trackChart) { window._trackChart.destroy(); }
                const ctx = chartCanvas.getContext('2d');
                window._trackChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: points.map(p => p.date.slice(5)),
                        datasets: [{
                            label: '累计收益 %',
                            data: points.map(p => p.cum_ret),
                            borderColor: '#ef5350',
                            backgroundColor: (ctx) => {
                                const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, 220);
                                g.addColorStop(0, 'rgba(239,83,80,0.15)');
                                g.addColorStop(1, 'rgba(239,83,80,0.01)');
                                return g;
                            },
                            fill: true,
                            tension: 0.3,
                            pointRadius: 2,
                            pointHoverRadius: 5,
                            borderWidth: 2,
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: true,
                        aspectRatio: 5,
                        plugins: {
                            legend: { display: false },
                            tooltip: {
                                callbacks: {
                                    label: (ctx) => `${ctx.parsed.y.toFixed(1)}%`
                                }
                            }
                        },
                        scales: {
                            x: {
                                ticks: { font: { size: 10 }, maxTicksLimit: 10 },
                                grid: { display: false }
                            },
                            y: {
                                ticks: { font: { size: 10 }, callback: (v) => v.toFixed(0) + '%' },
                                grid: { color: 'rgba(0,0,0,0.05)' }
                            }
                        }
                    }
                });
            } else {
                document.getElementById('curveSummary').innerHTML = '暂无收益数据（模拟建仓尚未开始）';
            }
        } catch(e) {
            document.getElementById('curveSummary').innerHTML = '曲线加载失败';
        }
    } catch(e) {
        statsEl.innerHTML = `<div class="error">加载失败: ${h(e.message)}</div>`;
        histEl.innerHTML = '';
    }
}


// ==================== ML扫描 (V11.0) ====================
async function doMLScan() {
    const el = document.getElementById('scanResult');
    if (!el) return;
    el.innerHTML = '<div class="loading"><span class="spinner"></span>正在扫描...</div>';
    try {
        const sortEl = document.getElementById('scanSort');
        const limit = sortEl ? parseInt(sortEl.value.replace('top', '')) : 10;
        const res = await fetch('/api/recommend?force_refresh=true&top_n=' + limit, {credentials: 'include'});
        const data = await res.json();
        const stocks = data['推荐股票'] || [];
        if (!stocks.length) {
            el.innerHTML = '<div class="empty">暂无结果</div>';
            return;
        }
        
        let html = `<div style="margin-bottom:8px; color:#8892b0; font-size:12px;">扫描时间: ${data['扫描时间']||''} | 策略: ${data['策略']||''} | 显示: ${stocks.length}只</div>`;
        html += '<div class="table-wrap"><table><thead><tr><th>#</th><th>代码</th><th>名称</th><th>行业</th><th>现价</th><th>涨跌幅</th><th>ML得分</th><th>排序强度</th><th>止损价</th><th>持仓建议</th><th>风险提示</th></tr></thead><tbody>';
        stocks.forEach((s, i) => {
            const chg = s.涨跌幅 || '0%';
            const chgColor = chg.includes('+') ? '#ef5350' : chg.includes('-') ? '#66bb6a' : '#8892b0';
            const suggestColor = s['持仓建议']=='2日短持' ? '#ffca28' : '#66bb6a';
            const riskColor = (s['风险提示']||'').includes('⚠️') ? '#ffca28' : '#66bb6a';
            html += `<tr>
                <td>${i+1}</td>
                <td><a href="${klineUrl(s.代码||'')}" target="_blank" style="color:#64b5f6;text-decoration:none;">${h(s.代码||'')} ↗</a></td>
                <td><a href="${klineUrl(s.代码||'')}" target="_blank" style="color:#e0e0e0;text-decoration:none;font-weight:bold;">${h(s.名称||'')} ↗</a></td>
                <td>${h(s.行业||'--')}</td>
                <td>${(s.现价||0).toFixed(2)}</td>
                <td style="color:${chgColor}">${h(chg)}</td>
                <td>${s.ML得分||'--'}</td>
                <td>${s.排序强度||'--'}</td>
                <td style="color:#ef5350;">${(s.止损价||0).toFixed(2)}</td>
                <td style="color:${suggestColor}">${h(s['持仓建议']||'5日持有')}</td>
                <td style="color:${riskColor};font-size:12px;">${h(s['风险提示']||'--')}</td>
            </tr>`;
        });
        html += '</tbody></table></div>';
        el.innerHTML = html;
    } catch(e) {
        el.innerHTML = `<div class="error">扫描失败: ${h(e.message)}</div>`;
    }
}

// ==================== 空状态HTML生成函数 ====================
function getEmptyStateHTML(icon, message, actionText, actionFunc) {
    return `<div class="empty-state">
        <div class="empty-icon">${icon}</div>
        <div class="empty-text">${message}</div>
        <button onclick="${actionFunc}">${actionText}</button>
    </div>`;
}

// ==================== 加载动画HTML ====================
function getLoadingHTML(message = '加载中...') {
    return `<div class="loading"><span class="spinner"></span>${message}</div>`;
}

// ==================== 绩效看板 ====================

// ==================== 指标卡片 ====================
function cardHTML(label, value, colorClass) {
    return `<div class="card"><div class="label">${label}</div><div class="value ${colorClass}">${value}</div></div>`;
}

// ==================== 跟单建议 ====================

// ========== 补充缺失的全局函数 ==========

// 退出登录
// ==================== 回测结果加载 ====================
async function loadBacktestSummary() {
    const el = document.getElementById('backtestSummary');
    const detailEl = document.getElementById('backtestDetail');
    if (!el) return;
    el.innerHTML = '<div class="loading"><span class="spinner"></span>加载回测结果...</div>';
    try {
        const res = await fetch('/data/backtest_pure_ml_v11_1.json');
        const data = await res.json();
        const rets = (data.v4ml || []).map(x => x.avg_ret);
        if (!rets.length) {
            el.innerHTML = '<div class="empty">暂无回测数据</div>';
            return;
        }
        const pos = rets.filter(r => r > 0).length;
        const neg = rets.filter(r => r < 0).length;
        const winRate = (pos / rets.length * 100).toFixed(1);
        const avgRet = (rets.reduce((a,b) => a+b, 0) / rets.length).toFixed(2);
        const avgPos = rets.filter(r => r > 0);
        const avgNeg = rets.filter(r => r < 0);
        const avgPosRet = avgPos.length ? (avgPos.reduce((a,b) => a+b, 0) / avgPos.length).toFixed(2) : '0';
        const avgNegRet = avgNeg.length ? (avgNeg.reduce((a,b) => a+b, 0) / avgNeg.length).toFixed(2) : '0';
        let cum = 100;
        rets.forEach(r => cum *= (1 + r/100));
        const totalRet = (cum - 100).toFixed(2);
        
        el.innerHTML = `
            <div class="card" style="padding:16px;">
                <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:12px;">
                    <div><div style="color:#8892b0;font-size:11px;">模型</div><div style="font-size:16px;font-weight:bold;">${data.model||'V11.0'}</div></div>
                    <div><div style="color:#8892b0;font-size:11px;">总交易</div><div style="font-size:16px;font-weight:bold;">${rets.length}次</div></div>
                    <div><div style="color:#8892b0;font-size:11px;">胜率</div><div style="font-size:16px;font-weight:bold;color:${winRate>=50?'#ef5350':'#66bb6a'}">${winRate}%</div></div>
                    <div><div style="color:#8892b0;font-size:11px;">累计收益</div><div style="font-size:16px;font-weight:bold;color:${totalRet>=0?'#ef5350':'#66bb6a'}">${totalRet}%</div></div>
                    <div><div style="color:#8892b0;font-size:11px;">平均单笔</div><div style="font-size:16px;font-weight:bold;">${avgRet}%</div></div>
                    <div><div style="color:#8892b0;font-size:11px;">平均盈利</div><div style="font-size:16px;font-weight:bold;color:#ef5350;">+${avgPosRet}%</div></div>
                    <div><div style="color:#8892b0;font-size:11px;">平均亏损</div><div style="font-size:16px;font-weight:bold;color:#66bb6a;">${avgNegRet}%</div></div>
                </div>
            </div>
        `;
        
        if (detailEl) {
            let html = '<div class="table-wrap" style="max-height:400px;overflow-y:auto;"><table><thead><tr><th>日期</th><th>平均收益</th><th>股票数</th></tr></thead><tbody>';
            (data.v4ml || []).slice(-30).forEach(r => {
                const c = r.avg_ret >= 0 ? '#ef5350' : '#66bb6a';
                html += `<tr><td>${r.date}</td><td style="color:${c}">${r.avg_ret >= 0 ? '+' : ''}${r.avg_ret.toFixed(2)}%</td><td>${r.n}</td></tr>`;
            });
            html += '</tbody></table></div>';
            detailEl.innerHTML = html;
        }
    } catch(e) {
        el.innerHTML = `<div class="error">加载失败: ${h(e.message)}</div>`;
    }
}


function doLogout() {
    window.location.href = '/logout';
}

// 信号记录加载
async function loadSignals() {
    try {
        const res = await apiFetch('/api/signals', {});
        const data = await res.json();
        const el = document.getElementById('signalsResult');
        if (data.error) {
            el.innerHTML = '<div class="error">' + h(data.error) + '</div>';
            return;
        }
        const signals = data.signals || data || [];
        if (!signals.length) {
            el.innerHTML = '<div class="empty">暂无信号记录</div>';
            return;
        }
        let html = '<div class="table-wrap"><table><thead><tr><th>类型</th><th>代码</th><th>名称</th><th>价格</th><th>数量</th><th>日期</th><th>理由</th><th>状态</th></tr></thead><tbody>';
        for (const s of signals) {
            html += '<tr>'
                + '<td>' + h(s.signal_type || s.type || '') + '</td>'
                + '<td>' + h((s.ts_code || s.code || '').split('.')[0]) + '</td>'
                + '<td>' + h(s.stock_name || s.name || '') + '</td>'
                + '<td>' + (s.price ? parseFloat(s.price).toFixed(2) : '-') + '</td>'
                + '<td>' + (s.qty || '-') + '</td>'
                + '<td>' + h(s.signal_date || s.date || '') + '</td>'
                + '<td>' + h(s.reason || '') + '</td>'
                + '<td>' + h(s.status || '') + '</td>'
                + '</tr>';
        }
        html += '</tbody></table></div>';
        el.innerHTML = html;
    } catch (e) {
        document.getElementById('signalsResult').innerHTML = '<div class="error">加载失败: ' + h(e.message) + '</div>';
    }
}

// 信号记录按钮
// A股交易入口（跳转到中信证券交易页面）
// 融资融券入口（跳转到中信证券信用交易页面）
// 信号标签样式匹配 — 更新后端返回格式
function getSignalTag(signal) {
    const map = {
        '止盈': '<span class="tag tag-red">止盈</span>',
        '止损': '<span class="tag tag-green">止损</span>',
        '关注': '<span class="tag tag-yellow">关注</span>',
        '持有': '<span class="tag tag-blue">持有</span>',
        '数据异常': '<span class="tag">--</span>',
    };
    // 支持后端带 emoji 的格式：️ 触发止盈 /  触发止损 /  浮动盈利≥5%
    if (signal && signal.includes('止盈')) return map['止盈'];
    if (signal && signal.includes('止损')) return map['止损'];
    if (signal && (signal.includes('盈利') || signal.includes('关注'))) return map['关注'];
    if (signal && signal.includes('持有')) return map['持有'];
    if (signal && signal.includes('异常')) return map['数据异常'];
    return map[signal] || '<span class="tag">' + h(signal) + '</span>';
}
