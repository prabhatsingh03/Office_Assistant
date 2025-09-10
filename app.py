import os
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
import time
from flask import Flask, redirect, url_for, session, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from msal import ConfidentialClientApplication
from dotenv import load_dotenv
import requests
import google.generativeai as genai

# Load environment variables
load_dotenv()

# --- App Initialization ---
app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret-key-for-dev")
# Dev session settings: allow cookies on current host, non-secure over http, Lax same-site
app.config['SESSION_COOKIE_DOMAIN'] = None
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False
app.config['PREFERRED_URL_SCHEME'] = 'http'

# --- Database Configuration ---
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'assistant.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Gemini AI Configuration ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# --- Database Models ---
class Priority(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    def to_dict(self):
        return {"id": self.id, "text": self.text}

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    health = db.Column(db.Integer, default=100)
    risk = db.Column(db.String(200), default="None major")
    action = db.Column(db.String(200), default="Monitor progress")
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    def to_dict(self):
        return {"id": self.id, "name": self.name, "health": self.health, "risk": self.risk, "action": self.action}

class Meeting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    time = db.Column(db.String(10), nullable=False)  # HH:MM format
    title = db.Column(db.String(200), nullable=False)
    location = db.Column(db.String(100), default="VC")
    brief = db.Column(db.String(500), default="")
    critical = db.Column(db.Boolean, default=False)
    date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    def to_dict(self):
        return {"id": self.id, "time": self.time, "title": self.title, "location": self.location, 
                "brief": self.brief, "critical": self.critical, "date": self.date.isoformat() if self.date else None}

class Protocol(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    gov = db.Column(db.Boolean, default=False)
    intl = db.Column(db.Boolean, default=True)
    notes = db.Column(db.Text, default="")
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    def to_dict(self):
        return {"id": self.id, "gov": self.gov, "intl": self.intl, "notes": self.notes}

class TimeSplit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bd = db.Column(db.Integer, default=40)
    internal = db.Column(db.Integer, default=35)
    strategy = db.Column(db.Integer, default=15)
    admin = db.Column(db.Integer, default=10)
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    def to_dict(self):
        return {"id": self.id, "BD": self.bd, "Internal": self.internal, "Strategy": self.strategy, "Admin": self.admin}

class DailyBrief(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    brief_content = db.Column(db.Text, nullable=False)
    decisions_required = db.Column(db.Text)
    drafts = db.Column(db.Text)
    followups = db.Column(db.Text)
    risks = db.Column(db.Text)
    next_actions = db.Column(db.Text)
    proton_update = db.Column(db.Text)  # JSON payload
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    def to_dict(self):
        return {"id": self.id, "date": self.date.isoformat() if self.date else None, 
                "brief_content": self.brief_content, "decisions_required": self.decisions_required,
                "drafts": self.drafts, "followups": self.followups, "risks": self.risks,
                "next_actions": self.next_actions, "proton_update": self.proton_update}

class LearningMemory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    context = db.Column(db.Text, nullable=False)
    correction = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), nullable=False)  # email_tone, brief_length, etc.
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    def to_dict(self):
        return {"id": self.id, "context": self.context, "correction": self.correction, 
                "category": self.category, "created_at": self.created_at.isoformat()}

# --- SQLite lightweight migrations (dev) ---
def _column_exists(table_name: str, column_name: str) -> bool:
    try:
        result = db.session.execute(db.text(f"PRAGMA table_info('{table_name}')"))
        for row in result:
            if str(row[1]).lower() == column_name.lower():
                return True
    except Exception:
        return False
    return False

def run_startup_migrations():
    # Add missing columns for existing dev databases
    # priority.created_at
    if not _column_exists('priority', 'created_at'):
        try:
            db.session.execute(db.text("ALTER TABLE priority ADD COLUMN created_at DATETIME"))
            db.session.commit()
        except Exception:
            db.session.rollback()
    # project.created_at, project.updated_at
    if not _column_exists('project', 'created_at'):
        try:
            db.session.execute(db.text("ALTER TABLE project ADD COLUMN created_at DATETIME"))
            db.session.commit()
        except Exception:
            db.session.rollback()
    if not _column_exists('project', 'updated_at'):
        try:
            db.session.execute(db.text("ALTER TABLE project ADD COLUMN updated_at DATETIME"))
            db.session.commit()
        except Exception:
            db.session.rollback()

# --- Microsoft Graph API Configuration ---
CLIENT_ID = os.getenv("MS_CLIENT_ID")
CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")
AUTHORITY = f"https://login.microsoftonline.com/{os.getenv('MS_TENANT_ID', 'common')}"
REDIRECT_PATH = "/api/outlook/callback"
# Force localhost so it matches Azure registered redirect
REDIRECT_URI = "https://office-assistant-7oam.onrender" + REDIRECT_PATH
# Use delegated Graph scopes only (Azure adds OIDC scopes automatically)
SCOPES = ["User.Read", "Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite"]
GRAPH_API_ENDPOINT = "https://graph.microsoft.com/v1.0"

# --- Helper Functions ---
def _get_msal_app():
    return ConfidentialClientApplication(CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET)

def _get_token_from_cache():
    return session.get("ms_graph_token")

# --- Routes ---
@app.route("/")
def index():
    """Serves the main HTML page."""
    return send_from_directory('.', 'index.html')

# --- Outlook Authentication Routes ---
@app.route("/api/outlook/auth")
def auth():
    session.permanent = True
    session["state"] = str(uuid.uuid4())
    auth_url = _get_msal_app().get_authorization_request_url(
        SCOPES,
        state=session["state"],
        redirect_uri=REDIRECT_URI
    )
    return redirect(auth_url)

@app.route(REDIRECT_PATH)
def callback():
    if request.args.get('state') != session.get("state"):
        return "State does not match.", 400
    if "error" in request.args:
        return f"Error: {request.args['error']}", 400
    if request.args.get('code'):
        result = _get_msal_app().acquire_token_by_authorization_code(
            request.args['code'],
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI
        )
        if result and result.get("access_token"):
            # Store a minimal token to keep cookie small
            session["ms_graph_token"] = {
                "access_token": result["access_token"],
                "expires_in": result.get("expires_in"),
                "expires_at": time.time() + float(result.get("expires_in", 0))
            }
            return redirect(url_for("index"))
        # Show MSAL error for easier debugging
        error_desc = result.get("error_description") if isinstance(result, dict) else ""
        return f"Authentication failed. {error_desc}", 400
    return "Authentication failed.", 400

# --- Outlook Status ---
@app.route('/api/outlook/status')
def outlook_status():
    token = _get_token_from_cache()
    return jsonify({"connected": bool(token)})

# --- Graph API Data Routes ---
def _make_graph_api_call(endpoint):
    token = _get_token_from_cache()
    if not token:
        return None, 401, "User not authenticated."
    headers = {
        'Authorization': f'Bearer {token.get("access_token")}',
        'Prefer': 'outlook.timezone="Asia/Kolkata"'
    }
    # Support both relative Graph endpoints and absolute nextLink URLs
    url = endpoint if isinstance(endpoint, str) and endpoint.startswith('http') else f"{GRAPH_API_ENDPOINT}{endpoint}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json(), 200, None
    return None, response.status_code, response.text

@app.route("/api/outlook/events")
def get_events():
    # Support either a specific date window (00:00-23:59) or a rolling last N hours window
    date_str = request.args.get('date')
    hours_param = request.args.get('hours')
    tz = ZoneInfo("Asia/Kolkata")
    if hours_param:
        try:
            hours = max(1, min(168, int(hours_param)))  # clamp between 1 hour and 7 days
        except ValueError:
            hours = 24
        end_dt = datetime.now(tz).replace(microsecond=0)
        start_dt = end_dt - timedelta(hours=hours)
    else:
        if date_str:
            day = datetime.fromisoformat(date_str).date()
        else:
            day = datetime.now(tz).date()
        start_dt = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        end_dt = datetime.combine(day, datetime.max.time().replace(microsecond=0), tzinfo=tz)

    start_q = quote(start_dt.isoformat(), safe='')
    end_q = quote(end_dt.isoformat(), safe='')

    query = f"/me/calendarView?startDateTime={start_q}&endDateTime={end_q}&$select=subject,location,start,end&$top=50"
    data, status, error = _make_graph_api_call(query)
    if error:
        return jsonify({"error": error}), status
    return jsonify(data)

@app.route('/api/outlook/mails')
def get_mails():
    query = "/me/messages?$top=10&$select=subject,from,receivedDateTime,webLink"
    data, status, error = _make_graph_api_call(query)
    if error:
        return jsonify({"error": error}), status
    return jsonify(data)

# --- Inbox Snapshot (Today) ---
@app.route('/api/inbox/snapshot', methods=['POST'])
def inbox_snapshot():
    if not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY is not configured on the server."}), 500

    # Fetch today's mails (IST) and summarize subjects + 1-line
    tz = ZoneInfo("Asia/Kolkata")
    hours_param = request.args.get('hours')
    if hours_param:
        try:
            hours = max(1, min(168, int(hours_param)))
        except ValueError:
            hours = 24
        end_dt = datetime.now(tz).replace(microsecond=0)
        start_dt = end_dt - timedelta(hours=hours)
    else:
        day = datetime.now(tz).date()
        start_dt = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        end_dt = datetime.combine(day, datetime.max.time().replace(microsecond=0), tzinfo=tz)

    # Using Graph filter over receivedDateTime in IST window
    filter_q = f"receivedDateTime ge {start_dt.isoformat()} and receivedDateTime le {end_dt.isoformat()}"
    query = f"/me/messages?$top=50&$select=id,subject,from,receivedDateTime,bodyPreview&$filter={quote(filter_q, safe=' <>:')}&$orderby=receivedDateTime desc"
    data, status, error = _make_graph_api_call(query)
    if error:
        return jsonify({"error": error}), status

    messages = data.get('value', []) if isinstance(data, dict) else []
    lines = []
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        snapshot_lines = []
        items = []
        for m in messages:
            subject = (m.get('subject') or '').strip()
            preview = (m.get('bodyPreview') or '').strip().replace('\n', ' ')
            sender_name = (m.get('from', {}).get('emailAddress', {}).get('name') or '').strip()
            msg_id = (m.get('id') or '').strip()
            when_raw = (m.get('receivedDateTime') or '').strip()
            # Normalize received time to IST explicitly
            try:
                iso = when_raw.replace('Z', '+00:00')
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_ist = dt.astimezone(ZoneInfo('Asia/Kolkata'))
                when_str = dt_ist.strftime('%Y-%m-%d %I:%M %p')
            except Exception:
                when_str = when_raw
            if not subject:
                continue
            # Summarize each mail individually
            per_mail_prompt = (
                "Summarize this email in one short line (<=20 words). "
                "Output only the summary sentence, no prefixes or labels.\n\n"
                f"Subject: {subject}\n"
                f"BodyPreview: {preview}"
            )
            try:
                resp = model.generate_content(per_mail_prompt)
                one_line = (getattr(resp, 'text', '') or '').strip()
                if not one_line:
                    one_line = preview[:160].strip()
                snapshot_lines.append(f"{subject} — {one_line} ({sender_name}, {when_str})")
                items.append({
                    "id": msg_id,
                    "subject": subject,
                    "sender": sender_name,
                    "received": when_str,
                    "summary": one_line
                })
            except Exception:
                fallback = preview[:160].strip()
                snapshot_lines.append(f"{subject} — {fallback} ({sender_name}, {when_str})")
                items.append({
                    "id": msg_id,
                    "subject": subject,
                    "sender": sender_name,
                    "received": when_str,
                    "summary": fallback
                })
        return jsonify({"snapshot": "\n".join(snapshot_lines) if snapshot_lines else "", "items": items})
    except Exception as e:
        return jsonify({"error": f"AI summarization failed: {str(e)}"}), 500

# Fetch full Outlook message by ID
@app.route('/api/outlook/message')
def get_message_by_id():
    msg_id = request.args.get('id')
    if not msg_id:
        return jsonify({"error": "Missing id"}), 400
    # Use $select to fetch body content and other details
    endpoint = f"/me/messages/{quote(msg_id, safe='')}?$select=subject,from,receivedDateTime,body,webLink"
    data, status, error = _make_graph_api_call(endpoint)
    if error:
        return jsonify({"error": error}), status
    return jsonify(data)

# --- Database API Routes ---
@app.route('/api/priorities', methods=['GET', 'POST'])
def handle_priorities():
    if request.method == 'GET':
        priorities = Priority.query.all()
        return jsonify([p.to_dict() for p in priorities])
    if request.method == 'POST':
        data = request.get_json()
        if not data or not data.get('text'):
            return jsonify({"error": "Priority text is required"}), 400
        new_priority = Priority(text=data['text'])
        db.session.add(new_priority)
        db.session.commit()
        return jsonify(new_priority.to_dict()), 201

@app.route('/api/priorities/<int:priority_id>', methods=['DELETE'])
def delete_priority(priority_id):
    priority = Priority.query.get_or_404(priority_id)
    db.session.delete(priority)
    db.session.commit()
    return jsonify({"message": "Priority deleted"}), 200

@app.route('/api/projects', methods=['GET', 'POST'])
def handle_projects():
    if request.method == 'GET':
        projects = Project.query.all()
        if not projects: # Seed with default data if empty
            defaults = [
                Project(name="PPL 5th Evaporator", health=62, risk="Delay: condenser delivery", action="Escalate vendor; recover 7 days via parallel E&I"),
                Project(name="Alkali Scrubber SAP-A/B", health=78, risk="None major", action="Lock FAT date; update drawings rev-C"),
                Project(name="TG-4 (23 MW)", health=54, risk="Civils lagging 2 weeks", action="Add 2nd crew; weekend shift"),
            ]
            db.session.bulk_save_objects(defaults)
            db.session.commit()
            projects = Project.query.all()
        return jsonify([p.to_dict() for p in projects])
    
    if request.method == 'POST':
        new_project = Project(
            name='New Project',
            health=75,
            risk='N/A',
            action='Define next steps'
        )
        db.session.add(new_project)
        db.session.commit()
        return jsonify(new_project.to_dict()), 201

@app.route('/api/projects/<int:project_id>', methods=['PUT', 'DELETE'])
def handle_project(project_id):
    project = Project.query.get_or_404(project_id)
    
    if request.method == 'PUT':
        data = request.get_json()
        project.name = data.get('name', project.name)
        project.health = data.get('health', project.health)
        project.risk = data.get('risk', project.risk)
        project.action = data.get('action', project.action)
        db.session.commit()
        return jsonify(project.to_dict())

    if request.method == 'DELETE':
        db.session.delete(project)
        db.session.commit()
        return jsonify({"message": "Project deleted"}), 200

# --- Meetings API Routes ---
@app.route('/api/meetings', methods=['GET', 'POST'])
def handle_meetings():
    if request.method == 'GET':
        date_str = request.args.get('date', datetime.now().date().isoformat())
        meetings = Meeting.query.filter_by(date=datetime.fromisoformat(date_str).date()).all()
        return jsonify([m.to_dict() for m in meetings])
    
    if request.method == 'POST':
        data = request.get_json()
        if not data or not data.get('title'):
            return jsonify({"error": "Meeting title is required"}), 400
        
        new_meeting = Meeting(
            time=data.get('time', '09:00'),
            title=data['title'],
            location=data.get('location', 'VC'),
            brief=data.get('brief', ''),
            critical=data.get('critical', False),
            date=datetime.fromisoformat(data.get('date', datetime.now().date().isoformat())).date()
        )
        db.session.add(new_meeting)
        db.session.commit()
        return jsonify(new_meeting.to_dict()), 201

@app.route('/api/meetings/<int:meeting_id>', methods=['PUT', 'DELETE'])
def handle_meeting(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    
    if request.method == 'PUT':
        data = request.get_json()
        meeting.time = data.get('time', meeting.time)
        meeting.title = data.get('title', meeting.title)
        meeting.location = data.get('location', meeting.location)
        meeting.brief = data.get('brief', meeting.brief)
        meeting.critical = data.get('critical', meeting.critical)
        if 'date' in data:
            meeting.date = datetime.fromisoformat(data['date']).date()
        db.session.commit()
        return jsonify(meeting.to_dict())
    
    if request.method == 'DELETE':
        db.session.delete(meeting)
        db.session.commit()
        return jsonify({"message": "Meeting deleted"}), 200

# --- Protocol API Routes ---
@app.route('/api/protocol', methods=['GET', 'PUT'])
def handle_protocol():
    if request.method == 'GET':
        protocol = Protocol.query.first()
        if not protocol:
            # Create default protocol
            protocol = Protocol(gov=False, intl=True, notes="Prep MoU protocol pack for Japanese delegation (Givery): seating, plaques, flag, photo-op).")
            db.session.add(protocol)
            db.session.commit()
        return jsonify(protocol.to_dict())
    
    if request.method == 'PUT':
        data = request.get_json()
        protocol = Protocol.query.first()
        if not protocol:
            protocol = Protocol()
            db.session.add(protocol)
        
        protocol.gov = data.get('gov', protocol.gov)
        protocol.intl = data.get('intl', protocol.intl)
        protocol.notes = data.get('notes', protocol.notes)
        db.session.commit()
        return jsonify(protocol.to_dict())

# --- Time Split API Routes ---
@app.route('/api/time-split', methods=['GET', 'PUT'])
def handle_time_split():
    if request.method == 'GET':
        time_split = TimeSplit.query.first()
        if not time_split:
            # Create default time split
            time_split = TimeSplit(bd=40, internal=35, strategy=15, admin=10)
            db.session.add(time_split)
            db.session.commit()
        return jsonify(time_split.to_dict())
    
    if request.method == 'PUT':
        data = request.get_json()
        time_split = TimeSplit.query.first()
        if not time_split:
            time_split = TimeSplit()
            db.session.add(time_split)
        
        time_split.bd = data.get('BD', time_split.bd)
        time_split.internal = data.get('Internal', time_split.internal)
        time_split.strategy = data.get('Strategy', time_split.strategy)
        time_split.admin = data.get('Admin', time_split.admin)
        db.session.commit()
        return jsonify(time_split.to_dict())

# --- Daily Brief API Routes ---
@app.route('/api/daily-briefs', methods=['GET', 'POST'])
def handle_daily_briefs():
    if request.method == 'GET':
        date_str = request.args.get('date', datetime.now().date().isoformat())
        brief = DailyBrief.query.filter_by(date=datetime.fromisoformat(date_str).date()).first()
        if brief:
            return jsonify(brief.to_dict())
        return jsonify({"message": "No brief found for this date"}), 404
    
    if request.method == 'POST':
        data = request.get_json()
        if not data or not data.get('brief_content'):
            return jsonify({"error": "Brief content is required"}), 400
        
        # Check if brief already exists for this date
        date_obj = datetime.fromisoformat(data.get('date', datetime.now().date().isoformat())).date()
        existing_brief = DailyBrief.query.filter_by(date=date_obj).first()
        
        if existing_brief:
            # Update existing brief
            existing_brief.brief_content = data['brief_content']
            existing_brief.decisions_required = data.get('decisions_required')
            existing_brief.drafts = data.get('drafts')
            existing_brief.followups = data.get('followups')
            existing_brief.risks = data.get('risks')
            existing_brief.next_actions = data.get('next_actions')
            existing_brief.proton_update = data.get('proton_update')
        else:
            # Create new brief
            new_brief = DailyBrief(
                date=date_obj,
                brief_content=data['brief_content'],
                decisions_required=data.get('decisions_required'),
                drafts=data.get('drafts'),
                followups=data.get('followups'),
                risks=data.get('risks'),
                next_actions=data.get('next_actions'),
                proton_update=data.get('proton_update')
            )
            db.session.add(new_brief)
        
        db.session.commit()
        return jsonify({"message": "Brief saved successfully"}), 201

# --- Learning Memory API Routes ---
@app.route('/api/learning-memory', methods=['GET', 'POST'])
def handle_learning_memory():
    if request.method == 'GET':
        category = request.args.get('category')
        memories = LearningMemory.query
        if category:
            memories = memories.filter_by(category=category)
        memories = memories.order_by(LearningMemory.created_at.desc()).limit(50).all()
        return jsonify([m.to_dict() for m in memories])
    
    if request.method == 'POST':
        data = request.get_json()
        if not data or not all(k in data for k in ['context', 'correction', 'category']):
            return jsonify({"error": "Context, correction, and category are required"}), 400
        
        new_memory = LearningMemory(
            context=data['context'],
            correction=data['correction'],
            category=data['category']
        )
        db.session.add(new_memory)
        db.session.commit()
        return jsonify(new_memory.to_dict()), 201

# --- AI Briefing Route ---
@app.route('/api/generate_brief', methods=['POST'])
def generate_brief():
    if not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY is not configured on the server."}), 500

    data = request.get_json()
    
    # Get learning memories for context
    memories = LearningMemory.query.order_by(LearningMemory.created_at.desc()).limit(10).all()
    memory_context = "\n".join([f"Context: {m.context}\nCorrection: {m.correction}" for m in memories])
    
    prompt = f"""SYSTEM
You are the AI Executive Assistant / Chief of Staff for the CEO of Simon India Limited i.e. Aashutosh Aggarwal. Optimize for clarity, brevity, anticipatory support, and diplomatic tone. Always output: {{decisions_required, drafts, followups, risks, next_actions}}.

LEARNING CONTEXT (CEO Corrections)
{memory_context if memory_context else "No previous corrections available."}

CONTEXT
Date: {data.get('date')}
Top priorities: {data.get('priorities')}
Inbox summary: {data.get('inboxSummary') or "(none provided)"}
Meetings: {data.get('meetings')}
Projects: {data.get('projects')}
Protocol: gov={data.get('protocol', {}).get('gov')}, intl={data.get('protocol', {}).get('intl')}, notes={data.get('protocol', {}).get('notes')}
Time-allocation target: BD {data.get('timeSplit', {}).get('BD')}%, Internal {data.get('timeSplit', {}).get('Internal')}%, Strategy {data.get('timeSplit', {}).get('Strategy')}%, Admin {data.get('timeSplit', {}).get('Admin')}%

TASKS
1) Produce a Morning CEO Brief (<=200 words)
2) Draft replies for critical emails (<=120 words each)
3) Create meeting briefs with bullets (context, last decisions, open issues, ask)
4) Update risk register suggestions

OUTPUT FORMAT
Please structure your response as follows:
BRIEF: [Your morning brief here]
DECISIONS_REQUIRED: [List of decisions needed]
DRAFTS: [Email drafts here]
FOLLOWUPS: [Follow-up actions]
RISKS: [Risk updates]
NEXT_ACTIONS: [Immediate next actions]"""

    try:
        # Use current Gemini model and resilient generation config
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)

        # Parse the response to extract structured data
        response_text = getattr(response, 'text', None) or (getattr(response, 'candidates', None) and str(response.candidates)) or ''
        brief_content = response_text
        
        # Try to extract structured components
        decisions_required = ""
        drafts = ""
        followups = ""
        risks = ""
        next_actions = ""
        
        # Simple parsing - in production, use a robust parser
        # Extract BRIEF content if present
        if "BRIEF" in response_text and "DECISIONS_REQUIRED" in response_text:
            try:
                brief_content = response_text.split("BRIEF", 1)[1]
                # remove optional colon
                brief_content = brief_content.split(":", 1)[1] if ":" in brief_content.split("DECISIONS_REQUIRED",1)[0] else brief_content
                brief_content = brief_content.split("DECISIONS_REQUIRED", 1)[0].strip()
            except Exception:
                brief_content = response_text
        if "DECISIONS_REQUIRED:" in response_text:
            decisions_required = response_text.split("DECISIONS_REQUIRED:")[1].split("DRAFTS:")[0].strip()
        if "DRAFTS:" in response_text:
            drafts = response_text.split("DRAFTS:")[1].split("FOLLOWUPS:")[0].strip()
        if "FOLLOWUPS:" in response_text:
            followups = response_text.split("FOLLOWUPS:")[1].split("RISKS:")[0].strip()
        if "RISKS:" in response_text:
            risks = response_text.split("RISKS:")[1].split("NEXT_ACTIONS:")[0].strip()
        if "NEXT_ACTIONS:" in response_text:
            next_actions = response_text.split("NEXT_ACTIONS:")[1].strip()
        
        return jsonify({
            'brief': brief_content,
            'decisions_required': decisions_required,
            'drafts': drafts,
            'followups': followups,
            'risks': risks,
            'next_actions': next_actions
        })
    except Exception as e:
        return jsonify({"error": f"An error occurred with the AI model: {str(e)}"}), 500


if __name__ == "__main__":
    with app.app_context():
        db.create_all() # Create database tables if they don't exist
        run_startup_migrations()
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5155)))
