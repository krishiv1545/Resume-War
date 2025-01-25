import re
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from models import db, User, Leaderboard
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from mira_sdk import MiraClient, Flow
import os
import json
# import fitz
import pdfplumber
from datetime import datetime, UTC


load_dotenv()

app = Flask(__name__)

app.secret_key = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///project_db.sqlite3'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db.init_app(app)

with app.app_context():
    db.create_all()


@app.route('/')
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('signup.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form['email']
        username = request.form['username']
        password = request.form['password']
        hashed_password = generate_password_hash(password)
        resume = request.files['resume']

        if resume and resume.filename:
            resume_path = os.path.join(
                app.config['UPLOAD_FOLDER'], resume.filename)
            resume.save(resume_path)

            try:
                new_user = User(email=email, username=username,
                                password=hashed_password, resume_path=resume_path)
                db.session.add(new_user)
                db.session.commit()
                flash('Signup successful! Please log in.', 'success')
                return redirect(url_for('home'))
            except Exception as e:
                flash('Username or email already exists.', 'error')
                print(e)
        else:
            flash('Please upload your resume.', 'error')
    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id
            return redirect(url_for('dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('login.html')


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        flash('Please log in.', 'error')
        return redirect(url_for('home'))

    user = User.query.get(session['user_id'])
    resume_path = user.resume_path
    print(resume_path)

    return render_template('dashboard.html', resume_path=resume_path)


@app.route('/uploads/<path:filename>')
def get_pdf(filename):
    return send_from_directory('uploads', filename)


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    flash('You have been logged out.', 'success')
    return redirect(url_for('home'))


@app.route('/leaderboard')
def leaderboard():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    current_user_id = session['user_id']
    leaderboard_data = Leaderboard.query.order_by(
        Leaderboard.rating.desc()).all()

    ranked_data = []
    for rank, entry in enumerate(leaderboard_data, start=1):
        user = User.query.get(entry.user_id)
        ranked_data.append({
            'rank': rank,
            'username': user.username,
            'score': entry.rating,
            'last_evaluated': entry.created_at,
            'is_current_user': entry.user_id == current_user_id
        })

    return render_template('leaderboard.html', leaderboard=ranked_data)


@app.route('/profile')
def profile():
    if 'user_id' not in session:
        flash('Please log in.', 'error')
        return redirect(url_for('home'))
    return render_template('profile.html')


client = MiraClient(config={"API_KEY": os.getenv("MIRA_API_KEY")})


def extract_text_from_pdf(pdf_path):
    """
    Reads all pages of the PDF and returns the text as a single string.
    """
    all_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            all_text.append(page_text)
    return "\n".join(all_text)


def preprocess_latex_sections(text):
    """
    Converts lines like \section*{Education} into 'Education\n'
    so that the subsequent parsing can detect headings more easily.
    """
    text = re.sub(r"\\section\*{([^}]*)}", r"\1\n", text)
    return text


def split_into_sections(text):
    """
    Splits the resume text by recognized headings (Education, Projects, etc.).
    Returns a dictionary with keys like "education", "projects", etc.
    """
    headings_pattern = r"(?P<heading>(Education|Projects|Technical Skills|Achievements|Certifications|Experience|Work Experience|Skills))(?=\s|:|$)"
    matches = list(re.finditer(headings_pattern, text, flags=re.IGNORECASE))

    if not matches:
        return {"others": text.strip()}

    sections = {}
    last_index = 0
    last_heading = None

    for match in matches:
        heading = match.group("heading")
        heading_start = match.start("heading")

        # Save the text from after the last heading up to this heading
        if last_heading:
            content = text[last_index:heading_start].strip()
            key = last_heading.lower()
            sections[key] = content

        last_heading = heading
        # move index pointer after this heading
        last_index = match.end("heading")

    # Capture any text after the final heading
    if last_heading:
        content = text[last_index:].strip()
        key = last_heading.lower()
        sections[key] = content

    return sections


def parse_education(section_text):
    """
    Splits out each degree found in the 'Education' text block
    and returns them as a list of dicts with degree, branch, cgpa, etc.
    """
    results = []
    lines = [l.strip() for l in section_text.split("\n") if l.strip()]

    # Simple pattern for known degrees
    degree_keywords = r"(Bachelor of Engineering \(B\.E\.\)|Bachelor of Arts|Master|Diploma|B\.E\.|BTech|MTech|PhD|Associate|Minor in|Data Science)"
    for line in lines:
        degree_match = re.search(degree_keywords, line, re.IGNORECASE)
        if degree_match:
            degree_found = degree_match.group(0)
            # Check for a known branch
            branch_match = re.search(
                r"(Computer Engineering|Computer Science|Business|Liberal Arts|Programming)", line, re.IGNORECASE)
            branch_found = branch_match.group(0) if branch_match else "NULL"
            results.append({
                "degree": degree_found.strip(),
                "branch": branch_found.strip(),
                "cgpa": "NULL"
            })

    return results if results else []


def parse_projects(section_text):
    """
    Parses projects by searching for lines or bullet points. This example expects lines like:
      ProjectName | TechStack
      Description lines...
    Returns a list of dicts with keys name, technologies, description.
    """
    projects = []
    lines = [l.strip() for l in section_text.split("\n") if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]
        # Attempt to detect "ProjectName | Technologies"
        if " | " in line:
            parts = line.split("|")
            name = parts[0].strip()
            tech = parts[1].strip() if len(parts) > 1 else ""
            i += 1

            desc_lines = []
            while i < len(lines) and " | " not in lines[i]:
                desc_lines.append(lines[i])
                i += 1

            description = " ".join(desc_lines)
            projects.append(
                {"name": name, "technologies": tech, "description": description})
        else:
            i += 1

    return projects


def parse_technical_skills(section_text):
    """
    Splits the text by lines for storing technical skills.
    """
    lines = [l.strip() for l in section_text.split("\n") if l.strip()]
    return lines


def parse_certifications(section_text):
    """
    Splits the text by lines for storing certifications.
    """
    lines = [l.strip() for l in section_text.split("\n") if l.strip()]
    return lines


def parse_achievements(section_text):
    """
    Splits the text by lines for storing achievements.
    """
    lines = [l.strip() for l in section_text.split("\n") if l.strip()]
    return lines


def parse_experience(section_text):
    """
    Splits the text by lines for storing experience entries.
    """
    lines = [l.strip() for l in section_text.split("\n") if l.strip()]
    return lines


def build_json_structure(sections_dict):
    """
    Uses the parsing functions to build a final dictionary
    containing all resume information in JSON format.
    """
    structured = {
        "education": [],
        "projects": [],
        "technical_skills": [],
        "achievements": [],
        "certifications": [],
        "experience": [],
        "others": []
    }

    for section, text_block in sections_dict.items():
        section_lower = section.lower()
        if section_lower == "education":
            structured["education"] = parse_education(text_block)
        elif section_lower == "projects":
            structured["projects"] = parse_projects(text_block)
        elif section_lower in ["technical skills", "skills"]:
            structured["technical_skills"].extend(
                parse_technical_skills(text_block))
        elif section_lower == "certifications":
            structured["certifications"] = parse_certifications(text_block)
        elif section_lower == "achievements":
            structured["achievements"] = parse_achievements(text_block)
        elif section_lower in ["experience", "work experience"]:
            structured["experience"] = parse_experience(text_block)
        else:
            # If it doesn't match known headings, dump into "others"
            other_lines = [l.strip()
                           for l in text_block.split("\n") if l.strip()]
            structured["others"].extend(other_lines)

    return structured


def parse_resume(pdf_path):
    """
    Complete workflow:
      1) Extract text from PDF
      2) Preprocess LaTeX sections
      3) Split text by recognized headings
      4) Parse each section
      5) Return final structured JSON
    """
    raw_text = extract_text_from_pdf(pdf_path)
    cleaned_text = preprocess_latex_sections(raw_text)
    sections = split_into_sections(cleaned_text)
    final_json = build_json_structure(sections)
    return final_json


@app.route('/evaluate_resume', methods=['GET', 'POST'])
def evaluate_resume():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    user = User.query.get(session['user_id'])

    if not user or not user.resume_path:
        return "Resume not found. Please upload a resume first.", 400

    try:
        pdf_path = user.resume_path
        parsed_output = parse_resume(pdf_path)
        user.parsed_resume = json.dumps(parsed_output)
        db.session.commit()

        # Redirect to miraflows after successful parsing
        return redirect(url_for('miraflows'))

    except Exception as e:
        print(f"Error parsing resume: {e}")
        return "An error occurred while processing the resume. Please try again.", 500


@app.route('/miraflows')
def miraflows():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    user = User.query.get(session['user_id'])
    parsed_resume = json.loads(user.parsed_resume)
    resume_path = user.resume_path

    try:
        flow = Flow(source="flow.yaml")
        input_dict = {"input": parsed_resume}
        response = client.flow.test(flow, input_dict)

        # Extract the JSON from the code block
        json_str = response['result']
        json_start = json_str.find('{')
        json_end = json_str.rfind('}') + 1
        extracted_json = json_str[json_start:json_end]

        # Parse the extracted JSON
        parsed_response = json.loads(extracted_json)

        analysis_text = "\n #### " + parsed_response['result']
        score = int(float(parsed_response['score']))  # Convert to float first

        existing_entry = Leaderboard.query.filter_by(user_id=user.id).first()

        if existing_entry:
            existing_entry.rating = score
            existing_entry.created_at = datetime.now(UTC)
        else:
            new_entry = Leaderboard(
                user_id=user.id,
                rating=score
            )
            db.session.add(new_entry)

        db.session.commit()

        return render_template('dashboard.html',
                               resume_data=parsed_resume,
                               mira_analysis=analysis_text,
                               resume_path=resume_path)

    except Exception as e:
        print(f"Mira Analysis Error: {e}")
        return render_template('dashboard.html',
                               resume_data=parsed_resume,
                               resume_path=resume_path)
  # add to git

    ############################################################################
    # FOR TESTING SO YOU DONT RUN OUT OF TOKENS, COMMENT BELOW AND UNCOMMENT TOP
    ############################################################################
    # THE MIRA ANALYSIS IS HARD CODED FOR TESTING PURPOSES ONLY, ENSURE TO COMMENT OUT THE BELOW CODE AND UNCOMMENT THE TOP CODE BEFORE DEPLOYMENT
    ############################################################################
    # REGARDS, KRISHIV KHAMBHAYATA (krishiv1545 on github/linkedin)
    ############################################################################

    """ user = User.query.get(session['user_id'])
    resume_path = user.resume_path
    parsed_resume = json.loads(user.parsed_resume)

    response = {'result': 'Based on the provided jsonified resume, here\'s a detailed analysis of each section with their respective ratings out of 10.00, followed by a total score out of 100:\n\n1. **Skills (Rate it out of 10.00):**\n - The candidate has a diverse set of technical skills across various programming languages, frameworks, tools, and libraries.\n - They possess knowledge in popular programming languages such as Java, Python, and JavaScript, as well as proficiency in web development frameworks like React and Flask.\n - Use of sophisticated developer tools like Docker and Git, and proficiency in data analysis libraries such as pandas and NumPy adds further credibility.\n - Skill Rating: **9.00/10.00**\n\n2. **Experience (Rate it out of 10.00):**\n - The candidate has relevant work experience in research positions and IT support, which demonstrates a mix of technical and problem-solving skills.\n - Experience with designing REST APIs and developing full-stack applications is particularly valuable.\n - However, the tenure of some positions such as the AI Research Assistant role was relatively short.\n - Experience Rating: **8.50/10.00**\n\n3. **Education (Rate it out of 10.00):**\n - The candidate possesses a Bachelor of Arts in Computer Science and an Associate degree in Liberal Arts.\n - There is, however, no GPA or specific academic achievements mentioned, which slightly diminishes the credibility of this section.\n - Education Rating: **7.50/10.00**\n\n4. **Certifications (Rate it out of 10.00):**\n - No certifications are listed in the resume, which is often important to substantiate skills with industry-recognized credentials.\n - Certifications Rating: **0.00/10.00**\n\n5. **Projects (Rate it out of 10.00):**\n - The candidate has undertaken significant projects such as Gitlytics and Simple Paintball.\n - The projects illustrate hands-on experience with various technologies, collaborative work, and practical problem-solving.\n - The impact of the "Simple Paintball" project with substantial downloads and positive feedback is notable.\n - Projects Rating: **9.00/10.00**\n\n6. **Achievements (Rate it out of 10.00):**\n - No specific achievements are explicitly listed in the achievements section.\n - However, some accomplishments could have been categorized under achievements but are currently stated in the context of experiences or projects.\n - Achievements Rating: **0.00/10.00**\n\n**Total Score Calculation:**\n\nTotal Score = \\( \\frac{9.00 + 8.50 + 7.50 + 0.00 + 9.00 + 0.00}{6} \\times 10 = \\frac{34.00}{6} \\times 10 \\)\n\nTotal Score: **56.67/100.00**\n\nThis analysis highlights strengths in skills and projects, while education and certifications could be improved to strengthen overall profile.'}
    analysis_text = "\n #### " + response['result']
    print(analysis_text)

    existing_entry = Leaderboard.query.filter_by(user_id=user.id).first()

    score = 77.9
    if existing_entry:
        existing_entry.rating = score
        existing_entry.created_at = datetime.now(UTC)
    else:
        new_entry = Leaderboard(user_id=user.id, rating=score)
        db.session.add(new_entry)
    db.session.commit()

    return render_template('dashboard.html', resume_data=parsed_resume, mira_analysis=analysis_text, resume_path=resume_path) """


if __name__ == '__main__':
    app.run(debug=True)
