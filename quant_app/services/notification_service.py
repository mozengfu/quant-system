"""
通知服务 - 短信、邮件、飞书通知
"""
import json
import logging
import uuid
from datetime import datetime

from quant_app.utils.config import (
    FEISHU_WEBHOOK, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS,
    ALIYUN_SMS_ACCESS_KEY, ALIYUN_SMS_ACCESS_SECRET, ALIYUN_SMS_SIGN_NAME
)

logger = logging.getLogger(__name__)


def send_sms(phone, template_code, template_param):
    """发送阿里云短信"""
    try:
        import urllib.request
        import urllib.parse
        import hmac
        import hashlib
        import base64

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
            "RegionId": "cn-hangzhou"
        }

        sorted_params = sorted(params.items())
        query_string = urllib.parse.urlencode(sorted_params)
        string_to_sign = f"GET&%2F&{urllib.parse.quote(query_string, safe='')}".replace("+", "%20").replace("*", "%2A").replace("%7E", "~")

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

        msg = MIMEText(content, 'plain', 'utf-8')
        msg['From'] = formataddr(("智能量化系统", SMTP_USER))
        msg['To'] = formataddr(("", to_email))
        msg['Subject'] = subject

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
    """发送飞书文本消息"""
    try:
        import urllib.request
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        data = json.dumps({"msg_type": "text", "content": {"text": message}}).encode('utf-8')
        req = urllib.request.Request(FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=5, context=ctx)
        result = json.loads(resp.read().decode())
        if result.get("StatusCode") == 0 or result.get("code") == 0:
            logger.info("飞书消息已发送")
        else:
            logger.warning(f"飞书发送返回异常: {result}")
        return True
    except Exception as e:
        logger.warning(f"飞书发送失败: {e}")
        return False
