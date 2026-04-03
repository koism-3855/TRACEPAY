from flask import Flask, request, jsonify, render_template, redirect, url_for, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from datetime import datetime, date
from functools import wraps
import random
import string
import csv
import io
import os

app = Flask(__name__)
CORS(app, supports_credentials=True)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///tracepay.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'tracepay-secret-key-2024')

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
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
    tier = db.Column(db.String(20), nullable=False)  # 元請/下請/孫請
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
            'created_at': self.created_at.isoformat()
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
    return [current_user.company_id] if current_user.company_id else []

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
    company_id = data.get('company_id')
    # First user ever becomes admin
    role = 'admin' if User.query.count() == 0 else 'member'
    user = User(email=email, role=role, company_id=company_id or None)
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
    if current_user.role != 'admin':
        company_id = current_user.company_id
    project = Project(
        code=code,
        name=data['name'],
        total_cost=data['total_cost'],
        status=data.get('status', '進行中'),
        start_date=datetime.strptime(data['start_date'], '%Y-%m-%d').date() if data.get('start_date') else None,
        end_date=datetime.strptime(data['end_date'], '%Y-%m-%d').date() if data.get('end_date') else None,
        company_id=company_id
    )
    db.session.add(project)
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
@admin_required
def create_company():
    data = request.json
    company = Company(
        name=data['name'],
        tier=data['tier'],
        payment_rate=data.get('payment_rate', 100.0),
        completion_rate=data.get('completion_rate', 100.0)
    )
    db.session.add(company)
    db.session.commit()
    return jsonify(company.to_dict()), 201

@app.route('/api/companies/<int:cid>', methods=['GET'])
@api_login_required
def get_company(cid):
    company = Company.query.get_or_404(cid)
    return jsonify(company.to_dict())

@app.route('/api/payments', methods=['POST'])
@api_login_required
def create_payment():
    data = request.json
    payment = Payment(
        project_id=data.get('project_id'),
        payer_id=data['payer_id'],
        payee_id=data['payee_id'],
        amount=data['amount'],
        scheduled_date=datetime.strptime(data['scheduled_date'], '%Y-%m-%d').date() if data.get('scheduled_date') else None,
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
    writer.writerow(['案件コード', '案件名', '総工事費', 'ステータス', '開始日', '終了日', '元請企業', '登録日'])
    for p in projects:
        writer.writerow([
            p.code, p.name, p.total_cost, p.status,
            p.start_date.isoformat() if p.start_date else '',
            p.end_date.isoformat() if p.end_date else '',
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

    payments_data = [
        (0, 1, 2, 50000000, today - timedelta(days=60), today - timedelta(days=55), '完了', 0),
        (0, 2, 3, 30000000, today - timedelta(days=45), today - timedelta(days=30), '遅延', 15),
        (0, 3, 4, 15000000, today - timedelta(days=30), None, '未払い', 0),
        (1, 1, 2, 40000000, today - timedelta(days=20), today - timedelta(days=18), '完了', 0),
        (1, 2, 5, 20000000, today - timedelta(days=10), None, '未払い', 0),
        (2, 1, 2, 25000000, today - timedelta(days=100), today - timedelta(days=65), '遅延', 35),
        (2, 2, 6, 12000000, today - timedelta(days=80), today - timedelta(days=79), '完了', 0),
        (3, 1, 3, 60000000, today + timedelta(days=30), None, '未払い', 0),
        (3, 3, 7, 18000000, today - timedelta(days=5), None, '未払い', 0),
        (4, 2, 4, 35000000, today - timedelta(days=50), None, '未払い', 0),
    ]
    for pi, payer_i, payee_i, amount, sched, actual, status, delay in payments_data:
        pay = Payment(
            project_id=projects[pi].id,
            payer_id=companies[payer_i].id,
            payee_id=companies[payee_i].id,
            amount=amount,
            scheduled_date=sched,
            actual_date=actual,
            status=status,
            delay_days=delay
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

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        seed_data()
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(port=port, debug=debug)
