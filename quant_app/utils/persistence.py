"""
JSON 文件持久化模块 - 统一管理所有 JSON 数据文件的读写操作
提供线程安全锁防止并发写入损坏
"""
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def _atomic_json_dump(data, filepath, **kwargs):
    """原子写入 JSON 文件：先写临时文件再 rename，防止写入中断导致文件损坏"""
    dirpath = filepath.parent
    dirpath.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix='.json', dir=str(dirpath))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, **kwargs)
        os.replace(tmp_path, str(filepath))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

from quant_app.utils.config import (
    ACCESS_LOG_FILE,
    PENDING_USERS_FILE,
    RESET_TOKENS_FILE,
    SESSIONS_FILE,
    SIGNALS_FILE,
    TRACK_FILE,
    USERS_FILE,
)

# 线程安全锁，防止多请求并发写入 JSON 文件导致数据损坏
_write_lock = threading.RLock()


# ========== 用户管理 ==========

def load_users():
    with _write_lock:
        if not USERS_FILE.exists():
            return {}
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)


def save_users(users):
    with _write_lock:
        _atomic_json_dump(users, USERS_FILE, indent=2)


# ========== 会话管理 ==========

def _load_sessions():
    """加载 sessions.json，过期自动清理"""
    with _write_lock:
        try:
            if SESSIONS_FILE.exists():
                with open(SESSIONS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                now = time.time()
                valid = {k: v for k, v in data.get("tokens", {}).items()
                         if v.get("expires", 0) > now}
                return valid
        except Exception as e:
            logger.warning("Sessions 加载失败: %s", e)
    return {}


def _save_sessions(tokens):
    """保存 sessions"""
    try:
        with _write_lock:
            _atomic_json_dump({"tokens": tokens}, SESSIONS_FILE, indent=2)
    except Exception as e:
        logger.warning("Sessions 保存失败: %s", e)


# ========== 访问日志 ==========

def get_client_ip(request):
    """获取客户端 IP"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _classify_module(action):
    """根据操作动作分类模块（合并版本）"""
    checks = [
        ("登录", "登录"), ("login", "登录"), ("logout", "登录"),
        ("分析个股", "个股分析"), ("个股", "个股分析"),
        ("策略选股", "策略选股"), ("扫描", "策略选股"), ("scan", "策略选股"), ("选股", "策略选股"),
        ("股票池扫描", "股票池"),
        ("强势活跃", "强势活跃"),
        ("底部起步", "底部起步"),
        ("持仓", "持仓管理"), ("position", "持仓管理"), ("止损", "持仓管理"),
        ("回测", "历史回测"), ("backtest", "历史回测"),
        ("信号", "信号记录"), ("signal", "信号记录"),
        ("技术面", "技术面"),
    ]
    for keyword, module in checks:
        if keyword in action:
            return module
    return "其他"


def _write_log_mysql(username, ip, action, module, timestamp_str):
    """写入 MySQL 日志"""
    import pymysql
    conn = None
    try:
        from quant_app.utils.config import get_db_config
        conn = pymysql.connect(**get_db_config())
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO system_logs (username, ip, action, module, timestamp) VALUES (%s, %s, %s, %s, %s)",
            (username, ip, action, module, timestamp_str)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.warning("MySQL日志写入失败: %s", e)
        return False
    finally:
        if conn:
            conn.close()


def save_access_log(username, ip="unknown", action="login"):
    """保存访问日志（MySQL + JSON 降级）"""
    try:
        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        module = _classify_module(action)

        # 优先写 MySQL（失败不阻塞）
        _write_log_mysql(username, ip, action, module, timestamp_str)

        # 始终保留 JSON 降级
        log_entry = {
            "username": username, "ip": ip, "action": action,
            "module": module, "timestamp": timestamp_str
        }
        with _write_lock:
            if ACCESS_LOG_FILE.exists():
                with open(ACCESS_LOG_FILE, encoding="utf-8") as f:
                    logs = json.load(f)
            else:
                logs = []
            logs.insert(0, log_entry)
            logs = logs[:1000]
            ACCESS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json_dump(logs, ACCESS_LOG_FILE, indent=2)
    except Exception as e:
        logger.warning("日志保存失败: %s", e)


# ========== 追踪/推荐 ==========

def load_track_data():
    """加载追踪数据"""
    with _write_lock:
        try:
            if TRACK_FILE.exists():
                with open(TRACK_FILE, encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning("追踪数据加载失败: %s", e)
    return {"recommendations": [], "stats": {"total": 0, "total_recommendations": 0}}


def save_track_data(data):
    """保存追踪数据"""
    try:
        with _write_lock:
            _atomic_json_dump(data, TRACK_FILE, indent=2)
    except Exception as e:
        logger.warning("追踪数据保存失败: %s", e)


def record_recommendation(stocks, strategy="C3.0 V3"):
    """记录每日推荐"""
    try:
        data = load_track_data()
        today = datetime.now().strftime("%Y-%m-%d")

        # 检查今天这个板块是否已记录
        for rec in data["recommendations"]:
            if rec["date"] == today and rec["strategy"] == strategy:
                return  # 今天这个板块已记录

        rec = {
            "date": today,
            "strategy": strategy,
            "stocks": [],
            "tracked": False,
            "result": None
        }

        for stock in stocks:
            rec["stocks"].append({
                "code": stock.get("代码", stock.get("code", "")),
                "name": stock.get("名称", stock.get("name", "")),
                "price": stock.get("现价", stock.get("price", 0)),
                "score": stock.get("综合评分", 0),
                "recommendation_time": datetime.now().strftime("%H:%M"),
                "1day_result": None,
                "1week_result": None,
                "1month_result": None
            })

        data["recommendations"].append(rec)
        data.setdefault("stats", {})
        data["stats"]["total_days"] = len(data["recommendations"])
        data["stats"]["total_recommendations"] = data["stats"].get("total_recommendations", 0) + len(stocks)

        save_track_data(data)
        logger.info("已记录 %s 只推荐股票", len(stocks))
    except Exception as e:
        logger.warning("记录推荐失败: %s", e)


TRACK_UPDATE_CACHE = {"last_update": 0, "cooldown": 300}  # 5分钟缓存


def _recalc_track_stats(data):
    """从已有推荐数据重算统计（不依赖 tracked 状态，总是全量重算）"""
    win_count = 0
    loss_count = 0
    total_profit = 0
    for rec in data.get("recommendations", []):
        for stock in rec.get("stocks", []):
            profit = stock.get("1month_result")
            if profit is None:
                profit = stock.get("1week_result")
            if profit is None:
                profit = stock.get("1day_result")
            if profit is not None and profit != 0:
                total_profit += profit
                if profit > 0:
                    win_count += 1
                else:
                    loss_count += 1

    total = win_count + loss_count
    if total > 0:
        data.setdefault("stats", {}).update({
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": round((win_count / total) * 100, 2),
            "avg_profit": round(total_profit / total, 2),
            "total_pnl": round(total_profit, 2),
        })
    return data


def update_stock_results():
    """更新股票后续表现（使用 MySQL daily_price 表计算真实交易日后收益）"""
    import pymysql
    data = load_track_data()

    # 先算一遍已有数据的统计（不依赖冷却，每次进来都算）
    data = _recalc_track_stats(data)

    data["stats"]["total_recommendations"] = data["stats"].get("total_recommendations", sum(len(r.get("stocks", [])) for r in data.get("recommendations", [])))
    save_track_data(data)

    now = time.time()
    if now - TRACK_UPDATE_CACHE["last_update"] < TRACK_UPDATE_CACHE["cooldown"]:
        return
    try:
        now_dt = datetime.now()

        conn = pymysql.connect(
            host=os.environ.get('MYSQL_HOST', 'localhost'),
            port=int(os.environ.get('MYSQL_PORT', 3306)),
            user=os.environ.get('MYSQL_USER', 'root'),
            password=os.environ.get('MYSQL_PASSWORD', ''),
            database=os.environ.get('MYSQL_DATABASE', 'quant_db'),
            charset='utf8mb4'
        )
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= DATE_SUB(CURDATE(), INTERVAL 2 YEAR) ORDER BY trade_date")
        trading_dates = [r[0] for r in cursor.fetchall()]
        trading_dates_str = sorted([d.strftime('%Y%m%d') for d in trading_dates])

        for rec in data["recommendations"]:
            if rec.get("tracked"):
                continue
            rec_date_str = rec["date"]

            for stock in rec["stocks"]:
                code = stock.get("code", "")
                if not code:
                    continue

                clean_code = code[2:] if code.upper().startswith(("SZ", "SH")) else code
                if clean_code.startswith("0") or clean_code.startswith("3"):
                    ts_code = f"{clean_code}.SZ"
                else:
                    ts_code = f"{clean_code}.SH"

                rec_price = stock.get("price", 0)
                if not rec_price or rec_price <= 0:
                    continue

                rec_date_fmt = rec_date_str.replace("-", "")
                try:
                    rec_idx = trading_dates_str.index(rec_date_fmt)
                except ValueError:
                    rec_idx = None
                    for i, td in enumerate(trading_dates_str):
                        if td > rec_date_fmt:
                            rec_idx = i
                            break
                    if rec_idx is None:
                        continue

                if stock.get("1day_result") is None:
                    target_idx = rec_idx + 1
                    if target_idx < len(trading_dates_str):
                        target_date = trading_dates_str[target_idx]
                        cursor.execute(
                            "SELECT close FROM daily_price WHERE ts_code=%s AND trade_date=%s",
                            (ts_code, target_date)
                        )
                        row = cursor.fetchone()
                        if row and row[0] and row[0] > 0:
                            stock["1day_result"] = round(((float(row[0]) - rec_price) / rec_price) * 100, 2)

                if stock.get("1week_result") is None:
                    target_idx = rec_idx + 5
                    if target_idx < len(trading_dates_str):
                        target_date = trading_dates_str[target_idx]
                        cursor.execute(
                            "SELECT close FROM daily_price WHERE ts_code=%s AND trade_date=%s",
                            (ts_code, target_date)
                        )
                        row = cursor.fetchone()
                        if row and row[0] and row[0] > 0:
                            stock["1week_result"] = round(((float(row[0]) - rec_price) / rec_price) * 100, 2)

                if stock.get("1month_result") is None:
                    target_idx = rec_idx + 20
                    if target_idx < len(trading_dates_str):
                        target_date = trading_dates_str[target_idx]
                        cursor.execute(
                            "SELECT close FROM daily_price WHERE ts_code=%s AND trade_date=%s",
                            (ts_code, target_date)
                        )
                        row = cursor.fetchone()
                        if row and row[0] and row[0] > 0:
                            stock["1month_result"] = round(((float(row[0]) - rec_price) / rec_price) * 100, 2)

                # 有任意周期结果即标记为已追踪
                has_any = (stock.get("1day_result") is not None or
                           stock.get("1week_result") is not None or
                           stock.get("1month_result") is not None)
                if has_any:
                    rec["tracked"] = True
                    rec["result"] = "completed" if stock.get("1month_result") is not None else "partial"

        conn.close()

        # 新数据更新后再算一次统计
        data = _recalc_track_stats(data)
        data["stats"]["total_recommendations"] = data["stats"].get("total_recommendations", sum(len(r.get("stocks", [])) for r in data.get("recommendations", [])))

        save_track_data(data)
        TRACK_UPDATE_CACHE["last_update"] = time.time()
    except Exception as e:
        logger.warning("更新结果失败: %s", e)


# ========== 待审批用户 ==========

def load_pending_users():
    try:
        if PENDING_USERS_FILE.exists():
            with open(PENDING_USERS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("待审批用户加载失败: %s", e)
    return {}


def save_pending_users(data):
    try:
        with _write_lock:
            _atomic_json_dump(data, PENDING_USERS_FILE, indent=2)
    except Exception as e:
        logger.warning("待审批用户保存失败: %s", e)


# ========== 密码重置令牌 ==========

def load_reset_tokens():
    try:
        if RESET_TOKENS_FILE.exists():
            with open(RESET_TOKENS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("重置令牌加载失败: %s", e)
    return {}


def save_reset_tokens(tokens=None):
    """保存重置令牌到文件。tokens 为 None 时保留已有文件内容。"""
    with _write_lock:
        if tokens is None:
            try:
                if RESET_TOKENS_FILE.exists():
                    tokens = json.loads(RESET_TOKENS_FILE.read_text())
                else:
                    tokens = {}
            except Exception:
                tokens = {}
        _atomic_json_dump(tokens, RESET_TOKENS_FILE, indent=2)


# ========== 信号记录 ==========

def get_signals_path():
    return SIGNALS_FILE


def read_signals():
    try:
        with open(SIGNALS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"signals": []}


def write_signals(sigs):
    with _write_lock:
        _atomic_json_dump({"signals": sigs}, SIGNALS_FILE, indent=2)


# ========== 持仓数据 ==========

def get_positions_data():
    """从MySQL数据库读取持仓数据"""
    try:
        import pymysql
        db_config = {
            'host': os.environ.get('MYSQL_HOST', 'localhost'),
            'unix_socket': os.environ.get('MYSQL_SOCKET', '/tmp/mysql.sock'),
            'user': os.environ.get('MYSQL_USER', 'root'),
            'password': os.environ.get('MYSQL_PASSWORD', ''),
            'database': os.environ.get('MYSQL_DATABASE', 'quant_db'),
            'connect_timeout': 3,
        }
        # 如果 socket 文件不存在，回退到 TCP 连接
        if not os.path.exists(db_config.get('unix_socket', '/tmp/mysql.sock')):
            db_config.pop('unix_socket')
            db_config['host'] = os.environ.get('MYSQL_HOST', '127.0.0.1')
            db_config['port'] = int(os.environ.get('MYSQL_PORT', 3306))

        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ts_code, code, name, market, quantity, cost,
                   stop_loss, take_profit, buy_date
            FROM positions
            ORDER BY buy_date DESC
        ''')
        positions = cursor.fetchall()
        conn.close()
        mapped = []
        for p in positions:
            mapped.append({
                "ts_code": p[0], "code": p[1], "name": p[2],
                "market": p[3], "quantity": float(p[4]), "cost": float(p[5]),
                "stop_loss": float(p[6]) if p[6] else 0,
                "take_profit": float(p[7]) if p[7] else 0,
                "buy_date": str(p[8]) if p[8] else "",
            })
        return mapped
    except Exception as e:
        logger.warning("MySQL持仓读取失败: %s", e)
        return []
