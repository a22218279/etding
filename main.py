import os
import imaplib
import email
import requests
from fastapi import FastAPI, HTTPException, Security, Depends, BackgroundTasks
from fastapi.security.api_key import APIKeyHeader, APIKey
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
from email.header import decode_header
import time
from datetime import datetime, timedelta
import pytz
from exchangelib import Credentials, Account, DELEGATE, Configuration
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
import urllib3
import hmac
import hashlib
import base64
import urllib.parse

# 禁用SSL警告
urllib3.disable_warnings()
BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 设置北京时区
beijing_tz = pytz.timezone('Asia/Shanghai')

# 配置检查间隔（秒）
CHECK_INTERVAL = 120  # 2分钟检查一次

# 服务状态
service_status = {
    "last_check_time": None,
    "last_check_status": "未开始",
    "error_count": 0,
    "consecutive_errors": 0,
    "is_checking": False
}

# 邮箱配置
def get_email_configs():
    configs = {
        'gmail': [],
        'qq': [],
        'outlook': []
    }
    
    # Gmail配置
    gmail_emails = os.getenv('GMAIL_EMAILS', '').split(',')
    gmail_passwords = os.getenv('GMAIL_PASSWORDS', '').split(',')
    for email, password in zip(gmail_emails, gmail_passwords):
        if email and password:
            configs['gmail'].append({
                'email': email.strip(),
                'password': password.strip()
            })
    
    # QQ邮箱配置
    qq_emails = os.getenv('QQ_EMAILS', '').split(',')
    qq_passwords = os.getenv('QQ_PASSWORDS', '').split(',')
    for email, password in zip(qq_emails, qq_passwords):
        if email and password:
            configs['qq'].append({
                'email': email.strip(),
                'password': password.strip()
            })
    
    # Outlook配置
    outlook_emails = os.getenv('OUTLOOK_EMAILS', '').split(',')
    outlook_passwords = os.getenv('OUTLOOK_PASSWORDS', '').split(',')
    for email, password in zip(outlook_emails, outlook_passwords):
        if email and password:
            configs['outlook'].append({
                'email': email.strip(),
                'password': password.strip()
            })
    
    return configs

app = FastAPI()

# API密钥验证
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def get_api_key(api_key_header: str = Security(api_key_header)):
    if api_key_header == os.getenv("API_KEY"):
        return api_key_header
    raise HTTPException(
        status_code=403,
        detail="无效的API密钥"
    )

def update_service_status(success: bool, error_message: str = None):
    """更新服务状态"""
    service_status["last_check_time"] = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
    
    if success:
        service_status["last_check_status"] = "成功"
        service_status["consecutive_errors"] = 0
    else:
        service_status["last_check_status"] = f"失败: {error_message}"
        service_status["error_count"] += 1
        service_status["consecutive_errors"] += 1

def send_test_message():
    webhook_url = os.getenv('WEIXIN_WEBHOOK')
    try:
        message = {
            "msgtype": "text",
            "text": {
                "content": "这是一条测试消息，来自邮件转发机器人",
                "mentioned_list": ["@all"]
            }
        }
        response = requests.post(webhook_url, json=message)
        if response.status_code == 200:
            return {"status": "success", "message": "测试消息发送成功"}
        else:
            return {"status": "error", "message": f"发送失败: {response.text}"}
    except Exception as e:
        return {"status": "error", "message": f"发送出错: {str(e)}"}

class EmailMonitor:
    def __init__(self, email_addr, password, imap_server, email_type):
        self.email_addr = email_addr
        self.password = password
        self.imap_server = imap_server
        self.email_type = email_type  # 'Gmail' 或 'QQ'
        self.weixin_webhook = os.getenv('WEIXIN_WEBHOOK')
        self.dingtalk_secret = os.getenv('DINGTALK_SECRET')
        self.dingtalk_webhook = os.getenv('DINGTALK_WEBHOOK')
        self.last_check_time = datetime.now(beijing_tz)

    def decode_subject(self, subject):
        if subject is None:
            return ""
        decoded_parts = []
        for part, encoding in decode_header(subject):
            if isinstance(part, bytes):
                try:
                    decoded_parts.append(part.decode(encoding or 'utf-8', errors='replace'))
                except:
                    decoded_parts.append(part.decode('utf-8', errors='replace'))
            else:
                decoded_parts.append(str(part))
        return ' '.join(decoded_parts)

    def connect(self):
        try:
            self.imap = imaplib.IMAP4_SSL(self.imap_server)
            self.imap.login(self.email_addr, self.password)
            return True
        except Exception as e:
            logger.error(f"连接邮箱失败: {str(e)}")
            return False

    def send_to_dingtalk(self, subject, sender, content, received_time, receiver):
        if not self.dingtalk_webhook:
            return
        try:
            if received_time.tzinfo is None:
                received_time = pytz.utc.localize(received_time)
            beijing_time = received_time.astimezone(beijing_tz)
            time_str = beijing_time.strftime("%Y-%m-%d %H:%M:%S")
    
            # 判断是否包含验证码
            if "[验证码]" in content:
                code = content.replace("[验证码] ", "")
                text = (
                    f"🔐 验证码通知\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📧 邮箱: {self.email_addr}\n"
                    f"📬 发件人: {sender}\n"
                    f"📬 转发人: {receiver}\n"
                    f"📋 主题: {subject}\n"
                    f"🔑 验证码: {code}\n"
                    f"⏰ 时间: {time_str}\n"
                    f"━━━━━━━━━━━━━━━"
                )
            else:
                text = (
                    f"📧 新邮件通知\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📧 邮箱: {self.email_addr}\n"
                    f"📬 发件人: {sender}\n"
                    f"📬 转发人: {receiver}\n"
                    f"📋 主题: {subject}\n"
                    f"📝 内容: {content[:200]}\n"
                    f"⏰ 时间: {time_str}\n"
                    f"━━━━━━━━━━━━━━━"
                )
    
            timestamp = str(round(time.time() * 1000))
            sign_str = f"{timestamp}\n{self.dingtalk_secret}".encode()
            hmac_code = hmac.new(self.dingtalk_secret.encode(), sign_str, digestmod=hashlib.sha256).digest()
            sign = base64.b64encode(hmac_code).decode()
    
            webhook_url = f"{self.dingtalk_webhook}&timestamp={timestamp}&sign={urllib.parse.quote_plus(sign)}"
    
            payload = {
                "msgtype": "text",
                "text": {"content": text}
            }
            resp = requests.post(webhook_url, json=payload, timeout=5)
            if resp.status_code == 200:
                logger.info(f"钉钉推送成功: {self.email_addr}")
            else:
                logger.error(f"钉钉推送失败: {resp.text}")
        except Exception as e:
            logger.error(f"钉钉推送异常: {str(e)}")


    def send_to_weixin(self, subject, sender, content, received_time):
        try:
            # 转换为北京时间
            if received_time.tzinfo is None:
                received_time = pytz.utc.localize(received_time)
            beijing_time = received_time.astimezone(beijing_tz)
            
            # 格式化北京时间
            time_str = beijing_time.strftime("%Y-%m-%d %H:%M:%S")
            
            # 根据邮箱类型设置不同的图标
            icon = "📧 Gmail" if self.email_type == "Gmail" else "📨 QQ邮箱"
            
            message = {
                "msgtype": "text",
                "text": {
                    "content": f"{icon}邮件通知\n\n📬 收件邮箱: {self.email_addr}\n⏰ 接收时间: {time_str} (北京时间)\n👤 发件人: {sender}\n📑 主题: {subject}\n\n📝 内容预览:\n{content}",
                    "mentioned_list": ["@all"]
                }
            }
            response = requests.post(
                self.weixin_webhook,
                json=message
            )
            if response.status_code == 200:
                logger.info(f"{self.email_type}邮件发送到微信成功")
            else:
                logger.error(f"{self.email_type}邮件发送到微信失败: {response.text}")
        except Exception as e:
            logger.error(f"{self.email_type}发送到微信时出错: {str(e)}")

    def extract_verification_code(self, email_message):
        """从邮件中提取验证码，支持多种格式"""
        import re
        
        # 获取邮件的纯文本和HTML内容
        text_content = ""
        html_content = ""
        
        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                try:
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue
                    charset = part.get_content_charset() or 'utf-8'
                    decoded = payload.decode(charset, errors='replace')
                    
                    if content_type == "text/plain":
                        text_content += decoded
                    elif content_type == "text/html":
                        html_content += decoded
                except:
                    continue
        else:
            try:
                html_content = email_message.get_payload(decode=True).decode(errors='replace')
            except:
                return None
        
        # 优先从纯文本提取
        content_to_search = text_content if text_content else html_content
        
        # 清理HTML标签（如果是HTML内容）
        if not text_content and html_content:
            clean = re.sub(r'<style[^>]*>.*?</style>', '', content_to_search, flags=re.DOTALL | re.IGNORECASE)
            clean = re.sub(r'<script[^>]*>.*?</script>', '', clean, flags=re.DOTALL | re.IGNORECASE)
            clean = re.sub(r'<[^>]+>', ' ', clean)
            clean = re.sub(r'&nbsp;|&lt;|&gt;|&amp;|&quot;', ' ', clean)
            clean = re.sub(r'\s+', ' ', clean).strip()
        else:
            clean = content_to_search
        
        # 验证码常见模式列表（按优先级排列）
        patterns = [
            # 模式1: verification code / 验证码 后面跟数字
            r'(?:verification\s*code|验证码|code|login\s*code|auth\s*code|security\s*code)[:\s]*(\d{4,8})',
            # 模式2: Your code is XXXXX
            r'(?:your\s+code\s+is|code\s+is|your\s+verification\s+code\s+is)[:\s]*(\d{4,8})',
            # 模式3: 单独一行或明显位置的6位数字
            r'(?:^|\n)\s*(\d{6})\s*(?:\n|$)',
            # 模式4: 大号字体显示的验证码（常见于HTML邮件）
            r'font-size[^>]*>\s*(\d{4,8})\s*<',
            # 模式5: 任何4-8位数字（兜底，但要过滤常见干扰项）
            r'\b(\d{4,8})\b',
        ]
        
        # 常见干扰数字（不是验证码的）
        ignore_numbers = {
            '2026', '0904', '1327', '0500', '0000', '1111', '1234', '4321',
            '2025', '2024', '2023', '2022', '2021', '2020',
            '1000', '2000', '3000', '5000', '9999',
            '123456', '654321', '000000', '111111', '222222', '333333',
            '444444', '555555', '666666', '777777', '888888', '999999'
        }
        
        for pattern in patterns:
            matches = re.findall(pattern, clean, re.IGNORECASE)
            for match in matches:
                # 确保是纯数字且在合理范围
                if match.isdigit() and len(match) >= 4 and len(match) <= 8:
                    if match not in ignore_numbers:
                        logger.info(f"从邮件中提取到验证码: {match}")
                        return match
        
        return None
        
    def get_email_content(self, email_message):
        """获取邮件内容，优先提取验证码"""
        import re
        # 先尝试提取验证码
        code = self.extract_verification_code(email_message)
        if code:
            return f"[验证码] {code}"

        # 没有验证码则返回纯文本预览
        content = ""
        if email_message.is_multipart():
            for part in email_message.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        content = part.get_payload(decode=True).decode(errors='replace')
                        break
                    except:
                        continue
        else:
            try:
                content = email_message.get_payload(decode=True).decode(errors='replace')
            except:
                content = "无法解析邮件内容"

        # 清理HTML标签
        clean = re.sub(r'<[^>]+>', ' ', content)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean[:500]  # 限制长度

    def check_emails(self):
        logger.info(f"开始检查{self.email_type}邮箱: {self.email_addr}")
        
        if not self.connect():
            return

        try:
            self.imap.select('INBOX')
            
            # QQ邮箱和Gmail使用不同的搜索条件
            if self.email_type == 'QQ':
                # QQ邮箱搜索最近天的未读邮件
                date = (datetime.now(beijing_tz) - timedelta(days=1)).strftime("%d-%b-%Y")
                _, messages = self.imap.search(None, f'(UNSEEN SINCE "{date}")')
            else:
                # Gmail使用时间过滤，使用北京时间
                date = (datetime.now(beijing_tz) - timedelta(minutes=30)).strftime("%d-%b-%Y")
                _, messages = self.imap.search(None, f'(UNSEEN SINCE "{date}")')
            
            message_count = len(messages[0].split())
            logger.info(f"发现 {message_count} 封新{self.email_type}邮件")
            
            for num in messages[0].split():
                try:
                    _, msg = self.imap.fetch(num, '(RFC822)')
                    email_body = msg[0][1]
                    email_message = email.message_from_bytes(email_body)
                    
                    # 获取邮件接收时间
                    date_str = email_message['date']
                    if date_str:
                        try:
                            # 解析邮件时间并转换为UTC时间
                            received_time = datetime.fromtimestamp(
                                email.utils.mktime_tz(
                                    email.utils.parsedate_tz(date_str)
                                ),
                                pytz.utc
                            )
                        except:
                            received_time = datetime.now(pytz.utc)
                    else:
                        received_time = datetime.now(pytz.utc)
                    
                    # 转换为北京时间进行比较
                    beijing_received_time = received_time.astimezone(beijing_tz)
                    time_diff = datetime.now(beijing_tz) - beijing_received_time
                    
                    # QQ邮箱处理最近24小时的邮件，Gmail处理最近30分钟的邮件
                    if (self.email_type == 'QQ' and time_diff > timedelta(days=1)) or \
                       (self.email_type == 'Gmail' and time_diff > timedelta(minutes=30)):
                        # 将超时的邮件标记为已读
                        self.imap.store(num, '+FLAGS', '\\Seen')
                        continue

                    subject = self.decode_subject(email_message['subject'])
                    sender = email_message['from']
                    receiver = email_message.get('to') or ''
                    content = self.get_email_content(email_message)

                    logger.info(f"发送{self.email_type}邮件到钉钉: {subject}")
                    # self.send_to_weixin(subject, sender, content, received_time)
                    self.send_to_dingtalk(subject, sender, content, received_time, receiver)
                    
                    # 发送成功后将邮件标记为已读
                    self.imap.store(num, '+FLAGS', '\\Seen')
                    
                except Exception as e:
                    logger.error(f"处理{self.email_type}邮件时出错: {str(e)}")
                    continue
                
        except Exception as e:
            logger.error(f"检查{self.email_type}邮件时出错: {str(e)}")
        finally:
            try:
                self.imap.close()
                self.imap.logout()
            except:
                pass

class OutlookMonitor:
    def __init__(self, email_addr, password):
        self.email_addr = email_addr
        self.password = password
        self.weixin_webhook = os.getenv('WEIXIN_WEBHOOK')
        self.last_check_time = datetime.now(beijing_tz)

    def connect(self):
        try:
            credentials = Credentials(self.email_addr, self.password)
            config = Configuration(credentials=credentials, server='outlook.office365.com')
            self.account = Account(
                primary_smtp_address=self.email_addr,
                config=config,
                access_type=DELEGATE
            )
            return True
        except Exception as e:
            logger.error(f"连接Outlook邮箱失败: {str(e)}")
            return False

    def send_to_weixin(self, subject, sender, content, received_time):
        try:
            # 转换为北京时间
            if received_time.tzinfo is None:
                received_time = pytz.utc.localize(received_time)
            beijing_time = received_time.astimezone(beijing_tz)
            
            # 格式化北京时间
            time_str = beijing_time.strftime("%Y-%m-%d %H:%M:%S")
            
            message = {
                "msgtype": "text",
                "text": {
                    "content": f"📨 Outlook邮件通知\n\n📬 收件邮箱: {self.email_addr}\n⏰ 接收时间: {time_str} (北京时间)\n👤 发件人: {sender}\n📑 主题: {subject}\n\n📝 内容预览:\n{content}",
                    "mentioned_list": ["@all"]
                }
            }
            response = requests.post(
                self.weixin_webhook,
                json=message
            )
            if response.status_code == 200:
                logger.info("Outlook邮件发送到微信成功")
            else:
                logger.error(f"Outlook邮件发送到微信失败: {response.text}")
        except Exception as e:
            logger.error(f"发送到微信时出错: {str(e)}")

    def check_emails(self):
        logger.info(f"开始检查Outlook邮箱: {self.email_addr}")
        
        if not self.connect():
            return

        try:
            # 获取最近30分钟的未读邮件
            filter_date = datetime.now(beijing_tz) - timedelta(minutes=30)
            unread_messages = self.account.inbox.filter(
                is_read=False,
                datetime_received__gt=filter_date
            )

            for message in unread_messages:
                try:
                    content = message.body[:500]  # 限制内容长度
                    self.send_to_weixin(
                        message.subject,
                        str(message.sender),
                        content,
                        message.datetime_received
                    )
                    message.is_read = True
                    message.save()
                except Exception as e:
                    logger.error(f"处理Outlook邮件时出错: {str(e)}")
                    continue

        except Exception as e:
            logger.error(f"检查Outlook邮件时出错: {str(e)}")

async def check_all_emails(background_tasks: BackgroundTasks):
    """检查所有配置的邮箱"""
    if service_status["is_checking"]:
        logger.info("已有检查任务在运行，跳过本次检查")
        return {"message": "邮件检查正在进行中"}
    
    service_status["is_checking"] = True
    configs = get_email_configs()
    
    try:
        # 检查Gmail邮箱
        for gmail_config in configs['gmail']:
            logger.info(f"开始检查Gmail邮箱: {gmail_config['email']}")
            monitor = EmailMonitor(
                gmail_config['email'],
                gmail_config['password'],
                'imap.gmail.com',
                'Gmail'
            )
            monitor.check_emails()
        
        # 检查QQ邮箱
        for qq_config in configs['qq']:
            logger.info(f"开始检查QQ邮箱: {qq_config['email']}")
            monitor = EmailMonitor(
                qq_config['email'],
                qq_config['password'],
                'imap.qq.com',
                'QQ'
            )
            monitor.check_emails()
        
        # 检查Outlook邮箱
        for outlook_config in configs['outlook']:
            logger.info(f"开始检查Outlook邮箱: {outlook_config['email']}")
            monitor = OutlookMonitor(
                outlook_config['email'],
                outlook_config['password']
            )
            monitor.check_emails()
        
        logger.info("所有邮箱检查完成")
        update_service_status(True)
    except Exception as e:
        error_message = f"检查邮件时出错: {str(e)}"
        logger.error(error_message)
        update_service_status(False, error_message)
    finally:
        service_status["is_checking"] = False

@app.get("/wake")
async def wake_service(background_tasks: BackgroundTasks):
    """唤醒服务并检查邮件（快速响应）"""
    # 立即返回响应
    background_tasks.add_task(process_wake_request)
    return {
        "message": "服务正常运行",
        "timestamp": datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "status": "accepted"
    }

async def process_wake_request():
    """后台处理wake请求"""
    if service_status["is_checking"]:
        logger.info("已有检查任务在运行，跳过本次检查")
        return
    
    logger.info("开始后台处理wake请求")
    service_status["is_checking"] = True
    
    try:
        # 检查环境变量
        configs = get_email_configs()
        logger.info(f"Gmail配置数量: {len(configs['gmail'])}")
        logger.info(f"QQ邮箱配置数量: {len(configs['qq'])}")
        logger.info(f"Outlook配置数量: {len(configs['outlook'])}")
        
        if not any([configs['gmail'], configs['qq'], configs['outlook']]):
            logger.error("没有找到有效的邮箱配置")
            return
        
        # if not os.getenv('WEIXIN_WEBHOOK'):
        #     logger.error("未配置微信Webhook")
        #     return
        
        # 检查Gmail邮箱
        for gmail_config in configs['gmail']:
            try:
                logger.info(f"检查Gmail邮箱: {gmail_config['email']}")
                monitor = EmailMonitor(
                    gmail_config['email'],
                    gmail_config['password'],
                    'imap.gmail.com',
                    'Gmail'
                )
                monitor.check_emails()
            except Exception as e:
                logger.error(f"Gmail邮箱检查失败: {str(e)}")
        
        # 检查QQ邮箱
        for qq_config in configs['qq']:
            try:
                logger.info(f"检查QQ邮箱: {qq_config['email']}")
                monitor = EmailMonitor(
                    qq_config['email'],
                    qq_config['password'],
                    'imap.qq.com',
                    'QQ'
                )
                monitor.check_emails()
            except Exception as e:
                logger.error(f"QQ邮箱检查失败: {str(e)}")
        
        # 检查Outlook邮箱
        for outlook_config in configs['outlook']:
            try:
                logger.info(f"检查Outlook邮箱: {outlook_config['email']}")
                monitor = OutlookMonitor(
                    outlook_config['email'],
                    outlook_config['password']
                )
                monitor.check_emails()
            except Exception as e:
                logger.error(f"Outlook邮箱检查失败: {str(e)}")
        
        logger.info("所有邮箱检查完成")
        update_service_status(True)
    except Exception as e:
        error_message = f"邮件检查过程出错: {str(e)}"
        logger.error(error_message)
        update_service_status(False, error_message)
    finally:
        service_status["is_checking"] = False

@app.get("/check", dependencies=[Depends(get_api_key)])
async def check_emails_endpoint(background_tasks: BackgroundTasks):
    """手动触发邮件检查"""
    return await check_all_emails(background_tasks)

@app.get("/status")
async def get_status():
    """获取服务状态"""
    return service_status

@app.get("/test")
async def test_webhook():
    """测试微信机器人"""
    return send_test_message()

@app.on_event("startup")
async def startup_event():
    async def keep_alive():
        while True:
            try:
                # 只保持服务活跃，不执行检查
                if os.getenv('VERCEL_URL'):
                    requests.get(f"https://{os.getenv('VERCEL_URL')}")
            except Exception as e:
                logger.error(f"keep-alive请求失败: {str(e)}")
            
            await asyncio.sleep(60)  # 每分钟ping一次
    
    # 创建keep-alive任务
    asyncio.create_task(keep_alive()) 

@app.get("/")
async def root():
    """根路由，显示服务基本信息"""
    return {
        "name": "邮件转发微信机器人",
        "status": "running",
        "version": "1.0.0",
        "endpoints": {
            "/": "服务信息",
            "/wake": "触发邮件检查（定时任务使用）",
            "/check": "手动触发检查（需要API密钥）",
            "/status": "查看服务状态",
            "/test": "测试微信机器人连接"
        },
        "last_check": service_status["last_check_time"],
        "is_checking": service_status["is_checking"]
    }

@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {
        "status": "healthy",
        "timestamp": datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
    }
