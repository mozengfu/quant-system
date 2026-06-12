"""
通知服务 - 短信、邮件、飞书通知
"""

import json
import logging
import time
import uuid
from collections import deque
from datetime import datetime

from quant_app.utils.config import (
    ALIYUN_SMS_ACCESS_KEY,
    ALIYUN_SMS_ACCESS_SECRET,
    ALIYUN_SMS_SIGN_NAME,
    FEISHU_WEBHOOK,
    SMTP_HOST,
    SMTP_PASS,
    SMTP_PORT,
    SMTP_USER,
    WECOM_WEBHOOK,
)

# 飞书发送限流:同消息 30s 内去重,60s 滑窗最多 10 条 (防 batch 触发爆量)
_FEISHU_RECENT = deque(maxlen=64)
_FEISHU_LAST_HASH = None
_FEISHU_LAST_TS = 0.0
_FEISHU_WINDOW = 60.0
_FEISHU_WINDOW_MAX = 10
_FEISHU_DEDUP_WINDOW = 30.0

logger = logging.getLogger(__name__)


def send_sms(phone, template_code, template_param):
    """发送阿里云短信"""
    try:
        import base64
        import hashlib
        import hmac
        import urllib.parse
        import urllib.request

        url = "https://dysmsapi.aliyuncs.com"

        params = {
            "AccessKeyId": ALIYUN_SMS_ACCESS_KEY,
            "Action": "SendSms",
            "SignName": ALIYUN_SMS_SIGN_NAME,
            "TemplateCode": template_code,
            "PhoneNumbers": phone,
            "TemplateParam": json.dumps(template_param),
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": "2017-05-25",
            "RegionId": "cn-hangzhou",
        }

        sorted_params = sorted(params.items())
        query_string = urllib.parse.urlencode(sorted_params)
        string_to_sign = (
            f"GET&%2F&{urllib.parse.quote(query_string, safe='')}".replace("+", "%20")
            .replace("*", "%2A")
            .replace("%7E", "~")
        )

        key = f"{ALIYUN_SMS_ACCESS_SECRET}&"
        signature = base64.b64encode(hmac.new(key.encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()
        params["Signature"] = signature

        full_url = f"{url}/?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full_url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("Code") == "OK":
                logger.info(f"短信已发送至 {phone}")
                return True
            else:
                logger.warning(f"短信发送失败: {result}")
                return False
    except Exception as e:
        logger.warning(f"短信发送失败: {e}")
        return False


def send_email(to_email, subject, content):
    """发送邮件通知"""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.utils import formataddr

        msg = MIMEText(content, "plain", "utf-8")
        msg["From"] = formataddr(("智能量化系统", SMTP_USER))
        msg["To"] = formataddr(("", to_email))
        msg["Subject"] = subject

        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [to_email], msg.as_string())
        server.quit()
        logger.info(f"邮件已发送至 {to_email}")
        return True
    except Exception as e:
        logger.warning(f"邮件发送失败: {e}")
        return False


def send_feishu(message):
    """发送飞书文本消息(带防爆量限流)

    防爆规则:
    1. 相同消息 30s 内只发一次 (de-dup)
    2. 60s 滑窗内最多 10 条 (rate-limit)
    被限流的不会真的发出 HTTP 请求,避免触发飞书频率限制。
    """
    global _FEISHU_LAST_HASH, _FEISHU_LAST_TS
    now = time.time()
    msg_hash = hash(message)

    # 1. 同消息 30s 内去重
    if msg_hash == _FEISHU_LAST_HASH and (now - _FEISHU_LAST_TS) < _FEISHU_DEDUP_WINDOW:
        logger.debug("飞书去重: 相同消息 30s 内已发送, 跳过 (msg=%r...)", message[:40])
        return False

    # 2. 60s 滑窗限流
    while _FEISHU_RECENT and (now - _FEISHU_RECENT[0][0]) > _FEISHU_WINDOW:
        _FEISHU_RECENT.popleft()
    if len(_FEISHU_RECENT) >= _FEISHU_WINDOW_MAX:
        logger.warning("飞书限流: 60s 内已发 %d 条, 跳过 (msg=%r...)",
                      len(_FEISHU_RECENT), message[:40])
        return False

    try:
        import ssl
        import urllib.request
        ssl_ctx = ssl._create_unverified_context()
        data = json.dumps({"msg_type": "text", "content": {"text": message}}).encode("utf-8")
        req = urllib.request.Request(FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=5, context=ssl_ctx)
        result = json.loads(resp.read().decode())
        if result.get("StatusCode") == 0 or result.get("code") == 0:
            logger.info("飞书消息已发送")
            _FEISHU_RECENT.append((now, msg_hash))
            _FEISHU_LAST_HASH = msg_hash
            _FEISHU_LAST_TS = now
        else:
            logger.warning(f"飞书发送返回异常: {result}")
        return True
    except Exception as e:
        logger.warning(f"飞书发送失败: {e}")
        return False


def send_wecom(message):
    """发送企业微信消息（通过群机器人 webhook）"""
    try:
        import urllib.request

        data = json.dumps({"msgtype": "text", "text": {"content": message}}).encode("utf-8")
        req = urllib.request.Request(WECOM_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read().decode())
        if result.get("errcode") == 0:
            logger.info("企业微信消息已发送")
        else:
            logger.warning(f"企业微信发送返回异常: {result}")
        return True
    except Exception as e:
        logger.warning(f"企业微信发送失败: {e}")
        return False
