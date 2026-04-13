from flask import Flask, request, jsonify, render_template, redirect, url_for, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, date, timedelta
from functools import wraps
import random
import string
import csv
import io
import os
import atexit

app = Flask(__name__)
CORS(app, supports_credentials=True)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///tracepay.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'tracepay-secret-key-2024')

# ── Flask-Mail (Gmail SMTP) ──
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME', 'noreply@tracepay.jp')

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
mail = Mail(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login_page'

# ─────────────────────────── Models ───────────────────────────

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=True)
    role = db.Column(db.String(20), default='member')  # superadmin / admin / member
    is_active = db.Column(db.Boolean, default=True)
    company = db.relationship('Company', backref='users')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_id(self):
        return str(self.id)

    @property
    def active(self):
        return self.is_active

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'role': self.role,
            'is_active': self.is_active,
            'company_id': self.company_id,
            'company_name': self.company.name if self.company else None,
            'created_at': self.created_at.isoformat(),
        }

class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    tier = db.Column(db.String(20), nullable=False)        # 元請/下請/孫請
    email = db.Column(db.String(120))                      # 催促メール送信先
    # ── 基本情報 ──
    representative = db.Column(db.String(100))             # 代表者名
    phone = db.Column(db.String(30))                       # 電話番号
    address = db.Column(db.String(200))                    # 住所
    # ── 建設業情報 ──
    license_number = db.Column(db.String(50))              # 建設業許可番号
    main_work_type = db.Column(db.String(200))             # 主要工種
    established_year = db.Column(db.Integer)               # 設立年
    capital = db.Column(db.Integer)                        # 資本金（万円）
    employees = db.Column(db.Integer)                      # 従業員数
    # ── 取引条件 ──
    payment_cycle = db.Column(db.Integer)                  # 支払いサイト（日）
    credit_limit = db.Column(db.Float)                     # 与信限度額（円）
    keishin_score = db.Column(db.Integer)                  # 経営事項審査P点
    # ── 信用スコア（自動計算） ──
    credit_score = db.Column(db.Float, default=100.0)
    credit_grade = db.Column(db.String(5), default='AAA')
    payment_rate = db.Column(db.Float, default=100.0)
    completion_rate = db.Column(db.Float, default=100.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'tier': self.tier,
            'email': self.email or '',
            'representative': self.representative or '',
            'phone': self.phone or '',
            'address': self.address or '',
            'license_number': self.license_number or '',
            'main_work_type': self.main_work_type or '',
            'established_year': self.established_year,
            'capital': self.capital,
            'employees': self.employees,
            'payment_cycle': self.payment_cycle,
            'credit_limit': self.credit_limit,
            'keishin_score': self.keishin_score,
            'credit_score': round(self.credit_score, 2),
            'credit_grade': self.credit_grade,
            'payment_rate': round(self.payment_rate, 2),
            'completion_rate': round(self.completion_rate, 2),
            'created_at': self.created_at.isoformat()
        }

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    total_cost = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='進行中')
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    invoice_date = db.Column(db.Date)      # 請求日
    transfer_date = db.Column(db.Date)     # 振込み日
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    company = db.relationship('Company', backref='projects')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self, include_payments=False):
        d = {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'total_cost': self.total_cost,
            'status': self.status,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'invoice_date': self.invoice_date.isoformat() if self.invoice_date else None,
            'transfer_date': self.transfer_date.isoformat() if self.transfer_date else None,
            'company_id': self.company_id,
            'company_name': self.company.name if self.company else None,
            'created_at': self.created_at.isoformat()
        }
        if include_payments:
            d['payments'] = [p.to_dict() for p in self.payments]
        return d

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'))
    project = db.relationship('Project', backref='payments')
    payer_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    payer = db.relationship('Company', foreign_keys=[payer_id])
    payee_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    payee = db.relationship('Company', foreign_keys=[payee_id])
    amount = db.Column(db.Float, nullable=False)
    scheduled_date = db.Column(db.Date)
    actual_date = db.Column(db.Date)
    status = db.Column(db.String(20), default='未払い')
    delay_days = db.Column(db.Integer, default=0)
    # 催促メール拡張フィールド
    invoice_date = db.Column(db.Date)
    due_date = db.Column(db.Date)
    payee_email = db.Column(db.String(120))
    reminder_count = db.Column(db.Integer, default=0)
    last_reminder_sent = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'project_code': self.project.code if self.project else None,
            'project_name': self.project.name if self.project else None,
            'payer_id': self.payer_id,
            'payer_name': self.payer.name if self.payer else None,
            'payee_id': self.payee_id,
            'payee_name': self.payee.name if self.payee else None,
            'amount': self.amount,
            'scheduled_date': self.scheduled_date.isoformat() if self.scheduled_date else None,
            'actual_date': self.actual_date.isoformat() if self.actual_date else None,
            'status': self.status,
            'delay_days': self.delay_days,
            'invoice_date': self.invoice_date.isoformat() if self.invoice_date else None,
            'due_date': self.due_date.isoformat() if self.due_date else None,
            'payee_email': self.payee_email,
            'reminder_count': self.reminder_count,
            'last_reminder_sent': self.last_reminder_sent.isoformat() if self.last_reminder_sent else None,
            'created_at': self.created_at.isoformat()
        }

class EmailLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey('payment.id'))
    payment = db.relationship('Payment', backref='email_logs')
    recipient = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(200))
    trigger_type = db.Column(db.String(50))   # 7日前/1日前/当日/3日超過/7日超過/14日超過/手動
    success = db.Column(db.Boolean, default=True)
    error_message = db.Column(db.Text)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'payment_id': self.payment_id,
            'project_name': self.payment.project.name if self.payment and self.payment.project else None,
            'recipient': self.recipient,
            'subject': self.subject,
            'trigger_type': self.trigger_type,
            'success': self.success,
            'error_message': self.error_message,
            'sent_at': self.sent_at.isoformat()
        }

class EmailTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trigger_type = db.Column(db.String(50), unique=True, nullable=False)
    subject_template = db.Column(db.String(200))
    body_template = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'trigger_type': self.trigger_type,
            'subject_template': self.subject_template,
            'body_template': self.body_template,
            'updated_at': self.updated_at.isoformat()
        }

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ─────────────────────────── Auth Helpers ───────────────────────────

def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'error': 'Unauthorized'}), 401
        if current_user.role != 'superadmin':
            return jsonify({'error': 'Superadmin only'}), 403
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'error': 'Unauthorized'}), 401
        if current_user.role not in ('admin', 'superadmin'):
            return jsonify({'error': 'Admin only'}), 403
        return f(*args, **kwargs)
    return decorated

def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'error': 'Unauthorized', 'redirect': '/login'}), 401
        if not current_user.is_active:
            logout_user()
            return jsonify({'error': 'アカウントが停止されています', 'redirect': '/login'}), 403
        return f(*args, **kwargs)
    return decorated

def visible_company_ids():
    """Return list of company IDs the current user may see. None = all."""
    if current_user.role in ('admin', 'superadmin'):
        return None
    # 会社未所属のメンバーは全データを閲覧可（制限なし）
    if not current_user.company_id:
        return None
    return [current_user.company_id]

# ─────────────────────────── Score Calc ───────────────────────────

def recalculate_score(company_id):
    company = Company.query.get(company_id)
    if not company:
        return
    payments = Payment.query.filter_by(payer_id=company_id).all()
    if not payments:
        return
    total = len(payments)
    completed = [p for p in payments if p.status == '完了']
    delayed = [p for p in payments if p.status == '遅延']
    unpaid = [p for p in payments if p.status == '未払い']
    payment_rate = len(completed) / total * 100 if total > 0 else 100
    avg_delay = sum(p.delay_days for p in delayed) / len(delayed) if delayed else 0
    penalty = len(unpaid) * 15 + len(delayed) * 5 + avg_delay * 0.3
    score = max(0, min(100, payment_rate - penalty))
    if score >= 90:
        grade = 'AAA'
    elif score >= 80:
        grade = 'AA'
    elif score >= 70:
        grade = 'A'
    elif score >= 50:
        grade = 'B'
    else:
        grade = 'C'
    company.credit_score = score
    company.credit_grade = grade
    company.payment_rate = payment_rate
    db.session.commit()

# ─────────────────────────── Email / Reminder ───────────────────────────

DEFAULT_TEMPLATES = {
    '7日前':   ('【TRACE PAY】{project}の支払い期日まで7日です',
                '※このメールはシステムから自動送信されています。\n\n{payer}様\n\n下記のお支払いの期日が7日後に迫っています。\n\n【案件名】{project}\n【支払金額】{amount}円\n【支払期日】{due_date}\n\nお早めにご準備をお願いいたします。\n\n支払い完了後は下記URLよりご報告ください：\n{complete_url}\n\n─────────────────\nTRACE PAY 自動催促システム'),
    '1日前':   ('【TRACE PAY】{project}の支払い期日は明日です',
                '※このメールはシステムから自動送信されています。\n\n{payer}様\n\n下記のお支払いの期日は明日です。\n\n【案件名】{project}\n【支払金額】{amount}円\n【支払期日】{due_date}\n\n明日までのお支払いをお願いいたします。\n\n支払い完了後は下記URLよりご報告ください：\n{complete_url}\n\n─────────────────\nTRACE PAY 自動催促システム'),
    '当日':    ('【TRACE PAY】{project}のお支払い期日は本日です',
                '※このメールはシステムから自動送信されています。\n\n{payer}様\n\n下記のお支払いの期日は本日です。\n\n【案件名】{project}\n【支払金額】{amount}円\n【支払期日】{due_date}\n\n本日中のお支払いをお願いいたします。\n\n支払い完了後は下記URLよりご報告ください：\n{complete_url}\n\n─────────────────\nTRACE PAY 自動催促システム'),
    '3日超過':  ('【TRACE PAY】{project}の支払いが{overdue}日超過しています',
                '※このメールはシステムから自動送信されています。\n\n{payer}様\n\n下記のお支払いが期日より{overdue}日超過しています。\n\n【案件名】{project}\n【支払金額】{amount}円\n【支払期日】{due_date}\n【超過日数】{overdue}日\n\n至急ご対応をお願いいたします。\n\n支払い完了後は下記URLよりご報告ください：\n{complete_url}\n\n─────────────────\nTRACE PAY 自動催促システム'),
    '7日超過':  ('【TRACE PAY】第二催促：{project}の支払いが{overdue}日超過',
                '※このメールはシステムから自動送信されています。\n\n{payer}様\n\n先日ご連絡いたしましたが、下記のお支払いがまだ完了していません。\n\n【案件名】{project}\n【支払金額】{amount}円\n【支払期日】{due_date}\n【超過日数】{overdue}日\n\n早急にご対応いただけますようお願いいたします。\n\n支払い完了後は下記URLよりご報告ください：\n{complete_url}\n\n─────────────────\nTRACE PAY 自動催促システム'),
    '14日超過': ('【TRACE PAY】最終警告：{project}の支払いが{overdue}日超過',
                '※このメールはシステムから自動送信されています。\n\n{payer}様\n\n下記のお支払いについて最終警告をお送りします。\n\n【案件名】{project}\n【支払金額】{amount}円\n【支払期日】{due_date}\n【超過日数】{overdue}日\n\nこれ以上のご対応がない場合、信用スコアに影響が生じます。\n至急ご連絡ください。\n\n支払い完了後は下記URLよりご報告ください：\n{complete_url}\n\n─────────────────\nTRACE PAY 自動催促システム'),
}

def get_template(trigger_type):
    t = EmailTemplate.query.filter_by(trigger_type=trigger_type).first()
    if t:
        return t.subject_template, t.body_template
    return DEFAULT_TEMPLATES.get(trigger_type, ('【TRACE PAY】お支払いのご確認', ''))

def send_reminder(payment, trigger_type, base_url='https://tracepay.onrender.com'):
    if not payment.payee_email:
        return False, 'メールアドレス未設定'
    due = payment.due_date or payment.scheduled_date
    overdue = (date.today() - due).days if due else 0
    complete_url = f'{base_url}/api/payments/{payment.id}/complete_link'
    project_name = payment.project.name if payment.project else '不明'
    payer_name = payment.payer.name if payment.payer else '不明'
    amount_str = f'{int(payment.amount):,}'
    due_str = due.strftime('%Y年%m月%d日') if due else '不明'
    subject_tpl, body_tpl = get_template(trigger_type)
    ctx = dict(project=project_name, payer=payer_name, amount=amount_str,
               due_date=due_str, overdue=overdue, complete_url=complete_url)
    subject = subject_tpl.format(**ctx)
    body = body_tpl.format(**ctx)
    log = EmailLog(payment_id=payment.id, recipient=payment.payee_email,
                   subject=subject, trigger_type=trigger_type)
    try:
        if not app.config.get('MAIL_USERNAME'):
            raise ValueError('MAIL_USERNAME が未設定です')
        msg = Message(subject=subject, recipients=[payment.payee_email], body=body)
        mail.send(msg)
        payment.reminder_count = (payment.reminder_count or 0) + 1
        payment.last_reminder_sent = date.today()
        log.success = True
        db.session.add(log)
        db.session.commit()
        return True, 'OK'
    except Exception as e:
        log.success = False
        log.error_message = str(e)
        db.session.add(log)
        db.session.commit()
        return False, str(e)

def check_and_send_reminders():
    """APScheduler が毎日9時に呼び出す関数"""
    with app.app_context():
        today = date.today()
        payments = Payment.query.filter(Payment.status != '完了').all()
        for p in payments:
            due = p.due_date or p.scheduled_date
            if not due or not p.payee_email:
                continue
            # 同日に複数回送らない
            if p.last_reminder_sent == today:
                continue
            days_left = (due - today).days
            overdue = (today - due).days
            trigger = None
            if days_left == 7:
                trigger = '7日前'
            elif days_left == 1:
                trigger = '1日前'
            elif days_left == 0:
                trigger = '当日'
            elif overdue == 3:
                trigger = '3日超過'
            elif overdue == 7:
                trigger = '7日超過'
            elif overdue == 14:
                trigger = '14日超過'
            if trigger:
                send_reminder(p, trigger)

# APScheduler 起動（gunicorn のワーカー重複起動を避けるため環境変数で制御）
if os.environ.get('DISABLE_SCHEDULER', '').lower() != 'true':
    scheduler = BackgroundScheduler(timezone='Asia/Tokyo')
    scheduler.add_job(check_and_send_reminders, CronTrigger(hour=9, minute=0))
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))

# ─────────────────────────── Page Routes ───────────────────────────

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/register')
def register_page():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    return render_template('register.html')

@app.route('/admin')
@login_required
def admin_page():
    if current_user.role != 'superadmin':
        return redirect(url_for('index'))
    return render_template('admin.html')

# ─────────────────────────── Auth API ───────────────────────────

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.json
    user = User.query.filter_by(email=data.get('email', '').lower()).first()
    if not user or not user.check_password(data.get('password', '')):
        return jsonify({'error': 'メールアドレスまたはパスワードが間違っています'}), 401
    login_user(user, remember=True)
    return jsonify({'user': user.to_dict()})

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.json
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    if not email or not password:
        return jsonify({'error': 'メールとパスワードは必須です'}), 400
    if len(password) < 6:
        return jsonify({'error': 'パスワードは6文字以上で設定してください'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'このメールアドレスはすでに登録されています'}), 409

    company_id = data.get('company_id') or None

    # 新規会社を同時作成する場合
    new_company_data = data.get('new_company')
    if new_company_data:
        company_name = new_company_data.get('name', '').strip()
        company_tier = new_company_data.get('tier', '元請')
        if not company_name:
            return jsonify({'error': '会社名を入力してください'}), 400
        new_company = Company(
            name=company_name,
            tier=company_tier,
            credit_score=100.0,
            credit_grade='AAA',
            payment_rate=100.0,
            completion_rate=100.0
        )
        db.session.add(new_company)
        db.session.flush()  # IDを確定させる
        company_id = new_company.id

    # 最初のユーザーは admin、以降は member
    role = 'admin' if User.query.count() == 0 else 'member'
    user = User(email=email, role=role, company_id=company_id)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    return jsonify({'user': user.to_dict()}), 201

@app.route('/api/auth/logout', methods=['POST'])
@login_required
def api_logout():
    logout_user()
    return jsonify({'ok': True})

@app.route('/api/auth/me', methods=['GET'])
def api_me():
    if not current_user.is_authenticated:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'user': current_user.to_dict()})

# ─────────────────────────── API Routes ───────────────────────────

@app.route('/api/projects', methods=['GET'])
@api_login_required
def get_projects():
    cids = visible_company_ids()
    if cids is None:
        projects = Project.query.order_by(Project.created_at.desc()).all()
    else:
        projects = Project.query.filter(Project.company_id.in_(cids)).order_by(Project.created_at.desc()).all()
    return jsonify([p.to_dict() for p in projects])

@app.route('/api/projects', methods=['POST'])
@api_login_required
def create_project():
    data = request.json
    code = 'PRJ-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    # members can only create under their own company
    company_id = data.get('company_id')
    if current_user.role not in ('admin', 'superadmin'):
        # 会社所属のメンバー → 自社に固定
        # 会社未所属のメンバー → フォームの選択値を使用
        if current_user.company_id:
            company_id = current_user.company_id
    project = Project(
        code=code,
        name=data['name'],
        total_cost=data['total_cost'],
        status=data.get('status', '進行中'),
        start_date=datetime.strptime(data['start_date'], '%Y-%m-%d').date() if data.get('start_date') else None,
        end_date=datetime.strptime(data['end_date'], '%Y-%m-%d').date() if data.get('end_date') else None,
        invoice_date=datetime.strptime(data['invoice_date'], '%Y-%m-%d').date() if data.get('invoice_date') else None,
        transfer_date=datetime.strptime(data['transfer_date'], '%Y-%m-%d').date() if data.get('transfer_date') else None,
        company_id=company_id
    )
    db.session.add(project)
    # 会社のメールアドレスを更新（入力された場合）
    payee_email = data.get('payee_email', '').strip()
    if payee_email and company_id:
        company = Company.query.get(company_id)
        if company:
            company.email = payee_email
    db.session.commit()
    return jsonify(project.to_dict()), 201

@app.route('/api/projects/<int:pid>', methods=['GET'])
@api_login_required
def get_project(pid):
    project = Project.query.get_or_404(pid)
    cids = visible_company_ids()
    if cids is not None and project.company_id not in cids:
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify(project.to_dict(include_payments=True))

@app.route('/api/companies', methods=['GET'])
@api_login_required
def get_companies():
    # All users can see all companies (for score reference), but members see full detail only for own
    companies = Company.query.order_by(Company.credit_score.desc()).all()
    return jsonify([c.to_dict() for c in companies])

@app.route('/api/companies', methods=['POST'])
@api_login_required
def create_company():
    data = request.json
    if not data.get('name'):
        return jsonify({'error': '企業名は必須です'}), 400
    if not data.get('email', '').strip():
        return jsonify({'error': 'メールアドレスは必須です'}), 400
    company = Company(
        name=data['name'],
        tier=data.get('tier', '元請'),
        email=data.get('email', '').strip() or None,
        payment_rate=data.get('payment_rate', 100.0),
        completion_rate=data.get('completion_rate', 100.0)
    )
    db.session.add(company)
    db.session.commit()
    return jsonify(company.to_dict()), 201

@app.route('/api/companies/<int:cid>', methods=['PATCH'])
@api_login_required
def update_company(cid):
    """会社情報を更新する"""
    company = Company.query.get_or_404(cid)
    data = request.json
    str_fields = ['name', 'tier', 'email', 'representative', 'phone', 'address',
                  'license_number', 'main_work_type']
    int_fields = ['established_year', 'capital', 'employees', 'payment_cycle', 'keishin_score']
    float_fields = ['credit_limit']
    for f in str_fields:
        if f in data:
            setattr(company, f, data[f].strip() or None)
    for f in int_fields:
        if f in data:
            setattr(company, f, int(data[f]) if data[f] not in (None, '', 0) else None)
    for f in float_fields:
        if f in data:
            setattr(company, f, float(data[f]) if data[f] not in (None, '') else None)
    if not company.name:
        return jsonify({'error': '企業名は必須です'}), 400
    db.session.commit()
    return jsonify(company.to_dict())

@app.route('/api/companies/<int:cid>', methods=['GET'])
@api_login_required
def get_company(cid):
    company = Company.query.get_or_404(cid)
    return jsonify(company.to_dict())

@app.route('/api/payments', methods=['POST'])
@api_login_required
def create_payment():
    data = request.json
    # payee_email: フォーム入力 → 会社登録メール → None の優先順位
    payee_email = data.get('payee_email', '').strip() or None
    if not payee_email and data.get('payee_id'):
        payee_company = Company.query.get(data['payee_id'])
        if payee_company and payee_company.email:
            payee_email = payee_company.email
    payment = Payment(
        project_id=data.get('project_id'),
        payer_id=data['payer_id'],
        payee_id=data['payee_id'],
        amount=data['amount'],
        scheduled_date=datetime.strptime(data['scheduled_date'], '%Y-%m-%d').date() if data.get('scheduled_date') else None,
        invoice_date=datetime.strptime(data['invoice_date'], '%Y-%m-%d').date() if data.get('invoice_date') else None,
        due_date=datetime.strptime(data['due_date'], '%Y-%m-%d').date() if data.get('due_date') else None,
        payee_email=payee_email,
        status=data.get('status', '未払い')
    )
    db.session.add(payment)
    db.session.commit()
    return jsonify(payment.to_dict()), 201

@app.route('/api/payments/<int:pid>', methods=['PATCH'])
@api_login_required
def update_payment(pid):
    payment = Payment.query.get_or_404(pid)
    # members can only update payments related to their company
    cids = visible_company_ids()
    if cids is not None and payment.payer_id not in cids and payment.payee_id not in cids:
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json
    if 'status' in data:
        payment.status = data['status']
    if data.get('status') == '完了':
        actual = date.today()
        payment.actual_date = actual
        if payment.scheduled_date:
            delta = (actual - payment.scheduled_date).days
            payment.delay_days = max(0, delta)
            if payment.delay_days > 0:
                payment.status = '遅延'
        recalculate_score(payment.payer_id)
    if 'actual_date' in data and data['actual_date']:
        payment.actual_date = datetime.strptime(data['actual_date'], '%Y-%m-%d').date()
    db.session.commit()
    return jsonify(payment.to_dict())

@app.route('/api/dashboard', methods=['GET'])
@api_login_required
def dashboard():
    cids = visible_company_ids()
    if cids is None:
        projects_q = Project.query
        payments_q = Payment.query
        companies = Company.query.all()
    else:
        projects_q = Project.query.filter(Project.company_id.in_(cids))
        payments_q = Payment.query.filter(
            (Payment.payer_id.in_(cids)) | (Payment.payee_id.in_(cids))
        )
        companies = Company.query.filter(Company.id.in_(cids)).all()

    total_projects = projects_q.count()
    active_projects = projects_q.filter_by(status='進行中').count()
    payments_list = payments_q.all()
    total_payments = len(payments_list)
    completed_payments = sum(1 for p in payments_list if p.status == '完了')
    delayed_payments = sum(1 for p in payments_list if p.status == '遅延')
    unpaid_payments = sum(1 for p in payments_list if p.status == '未払い')
    total_amount = sum(p.amount for p in payments_list)
    paid_amount = sum(p.amount for p in payments_list if p.status == '完了')
    unpaid_amount = sum(p.amount for p in payments_list if p.status == '未払い')
    grade_dist = {'AAA': 0, 'AA': 0, 'A': 0, 'B': 0, 'C': 0}
    all_companies_for_dist = Company.query.all() if cids is None else companies
    for c in all_companies_for_dist:
        grade_dist[c.credit_grade] = grade_dist.get(c.credit_grade, 0) + 1
    return jsonify({
        'total_projects': total_projects,
        'active_projects': active_projects,
        'total_payments': total_payments,
        'completed_payments': completed_payments,
        'delayed_payments': delayed_payments,
        'unpaid_payments': unpaid_payments,
        'total_amount': total_amount,
        'paid_amount': paid_amount,
        'unpaid_amount': unpaid_amount,
        'grade_distribution': grade_dist,
        'total_companies': len(Company.query.all())
    })

@app.route('/api/risk_alerts', methods=['GET'])
@api_login_required
def risk_alerts():
    cids = visible_company_ids()
    alerts = []
    today = date.today()

    unpaid_q = Payment.query.filter_by(status='未払い')
    if cids is not None:
        unpaid_q = unpaid_q.filter(
            (Payment.payer_id.in_(cids)) | (Payment.payee_id.in_(cids))
        )
    for p in unpaid_q.all():
        if p.scheduled_date and (today - p.scheduled_date).days > 0:
            days_overdue = (today - p.scheduled_date).days
            alerts.append({
                'type': 'overdue',
                'severity': 'high' if days_overdue > 30 else 'medium',
                'message': f'{p.payer.name if p.payer else "不明"}が{p.payee.name if p.payee else "不明"}への支払いが{days_overdue}日超過',
                'payment_id': p.id,
                'amount': p.amount,
                'days_overdue': days_overdue,
                'payer': p.payer.name if p.payer else None,
                'payee': p.payee.name if p.payee else None
            })

    low_score_q = Company.query.filter(Company.credit_score < 60)
    if cids is not None:
        low_score_q = low_score_q.filter(Company.id.in_(cids))
    for c in low_score_q.all():
        alerts.append({
            'type': 'low_score',
            'severity': 'high' if c.credit_score < 40 else 'medium',
            'message': f'{c.name}の信用スコアが低下（{c.credit_grade}: {round(c.credit_score, 1)}点）',
            'company_id': c.id,
            'company': c.name,
            'score': c.credit_score,
            'grade': c.credit_grade
        })
    alerts.sort(key=lambda x: 0 if x['severity'] == 'high' else 1)
    return jsonify(alerts)

# ─────────────────────────── Email API ───────────────────────────

@app.route('/api/emails/logs', methods=['GET'])
@api_login_required
def get_email_logs():
    cids = visible_company_ids()
    if cids is None:
        # admin/superadmin: 全件
        logs = EmailLog.query.order_by(EmailLog.sent_at.desc()).limit(200).all()
    else:
        # member: 自社が支払元/支払先の支払いに紐づくログのみ
        logs = (EmailLog.query
                .join(Payment, EmailLog.payment_id == Payment.id)
                .filter((Payment.payer_id.in_(cids)) | (Payment.payee_id.in_(cids)))
                .order_by(EmailLog.sent_at.desc())
                .limit(200).all())
    return jsonify([l.to_dict() for l in logs])

@app.route('/api/emails/send', methods=['POST'])
@admin_required
def manual_send_email():
    data = request.json
    payment_id = data.get('payment_id')
    trigger_type = data.get('trigger_type', '手動')
    payment = Payment.query.get_or_404(payment_id)
    ok, msg = send_reminder(payment, trigger_type)
    if ok:
        return jsonify({'ok': True, 'message': f'{payment.payee_email} へ送信しました'})
    return jsonify({'ok': False, 'error': msg}), 400

@app.route('/api/emails/templates', methods=['GET'])
@admin_required
def get_email_templates():
    result = []
    for trigger_type, (subj, body) in DEFAULT_TEMPLATES.items():
        t = EmailTemplate.query.filter_by(trigger_type=trigger_type).first()
        result.append({
            'trigger_type': trigger_type,
            'subject_template': t.subject_template if t else subj,
            'body_template': t.body_template if t else body,
            'is_customized': t is not None
        })
    return jsonify(result)

@app.route('/api/emails/templates/<trigger_type>', methods=['PUT'])
@admin_required
def update_email_template(trigger_type):
    if trigger_type not in DEFAULT_TEMPLATES:
        return jsonify({'error': '無効なトリガータイプです'}), 400
    data = request.json
    t = EmailTemplate.query.filter_by(trigger_type=trigger_type).first()
    if not t:
        t = EmailTemplate(trigger_type=trigger_type)
        db.session.add(t)
    t.subject_template = data.get('subject_template', '')
    t.body_template = data.get('body_template', '')
    t.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(t.to_dict())

@app.route('/api/emails/templates/<trigger_type>/reset', methods=['POST'])
@admin_required
def reset_email_template(trigger_type):
    t = EmailTemplate.query.filter_by(trigger_type=trigger_type).first()
    if t:
        db.session.delete(t)
        db.session.commit()
    return jsonify({'ok': True, 'message': 'デフォルトテンプレートに戻しました'})

@app.route('/api/payments/<int:pid>/complete_link', methods=['GET'])
def payment_complete_link(pid):
    """メール内のリンクから支払い完了処理"""
    payment = Payment.query.get_or_404(pid)
    if payment.status != '完了':
        payment.status = '完了'
        payment.actual_date = date.today()
        if payment.scheduled_date:
            delta = (payment.actual_date - payment.scheduled_date).days
            payment.delay_days = max(0, delta)
            if payment.delay_days > 0:
                payment.status = '遅延'
        recalculate_score(payment.payer_id)
    return render_template('complete.html', payment=payment)

# ─────────────────────────── Admin API ───────────────────────────

@app.route('/api/admin/stats', methods=['GET'])
@superadmin_required
def admin_stats():
    total_projects = Project.query.count()
    active_projects = Project.query.filter_by(status='進行中').count()
    total_companies = Company.query.count()
    total_users = User.query.count()
    active_users = User.query.filter_by(is_active=True).count()
    payments = Payment.query.all()
    total_amount = sum(p.amount for p in payments)
    unpaid_amount = sum(p.amount for p in payments if p.status == '未払い')
    avg_score = db.session.query(db.func.avg(Company.credit_score)).scalar() or 0
    return jsonify({
        'total_projects': total_projects,
        'active_projects': active_projects,
        'total_companies': total_companies,
        'total_users': total_users,
        'active_users': active_users,
        'total_amount': total_amount,
        'unpaid_amount': unpaid_amount,
        'avg_credit_score': round(avg_score, 2),
    })

@app.route('/api/admin/users', methods=['GET'])
@superadmin_required
def admin_list_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return jsonify([u.to_dict() for u in users])

@app.route('/api/admin/users/<int:uid>/suspend', methods=['PATCH'])
@superadmin_required
def admin_suspend_user(uid):
    user = User.query.get_or_404(uid)
    if user.id == current_user.id:
        return jsonify({'error': '自分自身は停止できません'}), 400
    user.is_active = not user.is_active
    db.session.commit()
    return jsonify(user.to_dict())

@app.route('/api/admin/users/<int:uid>/role', methods=['PATCH'])
@superadmin_required
def admin_change_role(uid):
    user = User.query.get_or_404(uid)
    data = request.json
    new_role = data.get('role')
    if new_role not in ('superadmin', 'admin', 'member'):
        return jsonify({'error': '無効なロールです'}), 400
    user.role = new_role
    db.session.commit()
    return jsonify(user.to_dict())

@app.route('/api/admin/invite', methods=['POST'])
@superadmin_required
def admin_invite():
    """Simulate sending an invite email — returns the invite link."""
    data = request.json
    email = data.get('email', '').lower().strip()
    if not email:
        return jsonify({'error': 'メールアドレスは必須です'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'このメールアドレスはすでに登録済みです'}), 409
    token = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    invite_url = f'/register?invite={token}&email={email}'
    # In production: send actual email here
    return jsonify({
        'ok': True,
        'message': f'{email} への招待リンクを生成しました（メール送信はデモ省略）',
        'invite_url': invite_url,
        'email': email,
    })

@app.route('/api/admin/export/projects', methods=['GET'])
@superadmin_required
def admin_export_projects():
    projects = Project.query.order_by(Project.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['案件コード', '案件名', '総工事費', 'ステータス', '開始日', '終了日', '請求日', '振込み日', '元請企業', '登録日'])
    for p in projects:
        writer.writerow([
            p.code, p.name, p.total_cost, p.status,
            p.start_date.isoformat() if p.start_date else '',
            p.end_date.isoformat() if p.end_date else '',
            p.invoice_date.isoformat() if p.invoice_date else '',
            p.transfer_date.isoformat() if p.transfer_date else '',
            p.company.name if p.company else '',
            p.created_at.strftime('%Y-%m-%d'),
        ])
    output.seek(0)
    return Response(
        '\ufeff' + output.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=projects.csv'}
    )

@app.route('/api/admin/export/payments', methods=['GET'])
@superadmin_required
def admin_export_payments():
    payments = Payment.query.order_by(Payment.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', '案件コード', '支払元', '支払先', '金額', '予定日', '実績日', 'ステータス', '遅延日数', '登録日'])
    for p in payments:
        writer.writerow([
            p.id,
            p.project.code if p.project else '',
            p.payer.name if p.payer else '',
            p.payee.name if p.payee else '',
            p.amount,
            p.scheduled_date.isoformat() if p.scheduled_date else '',
            p.actual_date.isoformat() if p.actual_date else '',
            p.status, p.delay_days,
            p.created_at.strftime('%Y-%m-%d'),
        ])
    output.seek(0)
    return Response(
        '\ufeff' + output.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=payments.csv'}
    )

@app.route('/api/admin/export/companies', methods=['GET'])
@superadmin_required
def admin_export_companies():
    companies = Company.query.order_by(Company.credit_score.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['企業名', '区分', '信用グレード', '信用スコア', '支払い完了率', '完遂率', '登録日'])
    for c in companies:
        writer.writerow([
            c.name, c.tier, c.credit_grade,
            round(c.credit_score, 2), round(c.payment_rate, 2), round(c.completion_rate, 2),
            c.created_at.strftime('%Y-%m-%d'),
        ])
    output.seek(0)
    return Response(
        '\ufeff' + output.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=companies.csv'}
    )

# ─────────────────────────── Seed Data ───────────────────────────

def seed_data():
    if Company.query.count() > 0:
        return
    companies_data = [
        ('大和建設株式会社', '元請', 95.0, 98.0),
        ('東洋工業株式会社', '元請', 88.0, 92.0),
        ('山田工務店', '下請', 72.0, 85.0),
        ('鈴木建築', '下請', 65.0, 78.0),
        ('田中土木', '下請', 55.0, 70.0),
        ('佐藤設備工業', '孫請', 90.0, 95.0),
        ('高橋電気工事', '孫請', 45.0, 60.0),
        ('中村塗装', '孫請', 80.0, 88.0),
        ('小林基礎工事', '孫請', 35.0, 55.0),
        ('渡辺鉄骨', '下請', 78.0, 82.0),
    ]
    companies = []
    for name, tier, payment_rate, completion_rate in companies_data:
        c = Company(name=name, tier=tier, payment_rate=payment_rate, completion_rate=completion_rate)
        db.session.add(c)
        companies.append(c)
    db.session.flush()

    from datetime import timedelta
    today = date.today()
    projects_data = [
        ('渋谷再開発プロジェクト', 500000000, '進行中', today - timedelta(days=90), today + timedelta(days=180)),
        ('港区オフィスビル建設', 320000000, '進行中', today - timedelta(days=60), today + timedelta(days=240)),
        ('横浜工場増設工事', 180000000, '完了', today - timedelta(days=200), today - timedelta(days=30)),
        ('新宿マンション建設', 420000000, '進行中', today - timedelta(days=30), today + timedelta(days=300)),
        ('川崎物流センター建設', 260000000, '中断', today - timedelta(days=120), today + timedelta(days=60)),
    ]
    projects = []
    for name, cost, status, start, end in projects_data:
        code = 'PRJ-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        p = Project(code=code, name=name, total_cost=cost, status=status,
                    start_date=start, end_date=end, company_id=companies[0].id)
        db.session.add(p)
        projects.append(p)
    db.session.flush()

    # (pi, payer_i, payee_i, amount, sched, actual, status, delay, due_date_offset, email)
    payments_data = [
        (0, 1, 2, 50000000, today-timedelta(days=60), today-timedelta(days=55), '完了', 0,  -55, 'yamada@toyokogyo.jp'),
        (0, 2, 3, 30000000, today-timedelta(days=45), today-timedelta(days=30), '遅延', 15, -30, 'suzuki@yamadakoumuten.jp'),
        (0, 3, 4, 15000000, today-timedelta(days=30), None,                     '未払い', 0, -30, 'tanaka@suzukikenchiku.jp'),
        (1, 1, 2, 40000000, today-timedelta(days=20), today-timedelta(days=18), '完了', 0,  -18, 'yamada@toyokogyo.jp'),
        (1, 2, 5, 20000000, today-timedelta(days=10), None,                     '未払い', 0, -10, 'nakamura@tanakadобоку.jp'),
        (2, 1, 2, 25000000, today-timedelta(days=100),today-timedelta(days=65), '遅延', 35, -65, 'yamada@toyokogyo.jp'),
        (2, 2, 6, 12000000, today-timedelta(days=80), today-timedelta(days=79), '完了', 0,  -79, 'sato@satosetsubi.jp'),
        (3, 1, 3, 60000000, today+timedelta(days=30), None,                     '未払い', 0, +30, 'suzuki@yamadakoumuten.jp'),
        (3, 3, 7, 18000000, today-timedelta(days=5),  None,                     '未払い', 0,  -5, 'takahashi@takahashidenki.jp'),
        (4, 2, 4, 35000000, today-timedelta(days=50), None,                     '未払い', 0, -50, 'tanaka@suzukikenchiku.jp'),
    ]
    for pi, payer_i, payee_i, amount, sched, actual, status, delay, due_offset, email in payments_data:
        pay = Payment(
            project_id=projects[pi].id,
            payer_id=companies[payer_i].id,
            payee_id=companies[payee_i].id,
            amount=amount,
            scheduled_date=sched,
            actual_date=actual,
            status=status,
            delay_days=delay,
            due_date=today + timedelta(days=due_offset),
            invoice_date=sched - timedelta(days=14) if sched else None,
            payee_email=email,
        )
        db.session.add(pay)
    db.session.commit()

    for c in companies:
        recalculate_score(c.id)

    # Seed demo accounts
    if User.query.count() == 0:
        superadmin = User(email='superadmin@tracepay.jp', role='superadmin', company_id=None)
        superadmin.set_password('super1234')
        db.session.add(superadmin)
        admin = User(email='admin@tracepay.jp', role='admin', company_id=companies[0].id)
        admin.set_password('admin1234')
        db.session.add(admin)
        member = User(email='member@tracepay.jp', role='member', company_id=companies[1].id)
        member.set_password('member1234')
        db.session.add(member)
        db.session.commit()

# gunicorn でも __main__ でも必ず実行されるようにモジュールレベルで初期化
with app.app_context():
    db.create_all()
    seed_data()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(port=port, debug=debug)
