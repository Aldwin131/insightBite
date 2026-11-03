import os
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from groq import Groq
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///insightbite.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
# Inisialisasi Groq Client
groq_client = Groq(
    api_key=os.getenv("GROQ_API_KEY"),
)

class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    teks_asli = db.Column(db.Text, nullable=False)
    sentimen = db.Column(db.String(50))
    topik = db.Column(db.String(100))
    draf_balasan = db.Column(db.Text)
    status_revisi = db.Column(db.String(100))

with app.app_context():
    db.create_all()

# --- FUNGSI PIPELINE MULTI-AGENT ---
def run_agentic_loop(review_text):
    # 1. Agen Classifier: Analisis Sentimen & Topik
    prompt_classifier = f"Analisis ulasan restoran ini: '{review_text}'. Tentukan Sentimen (Positif/Negatif) dan Topik Utama (Porsi, Rasa, Pelayanan, Harga). Balas HANYA dengan format pasti: Sentimen | Topik."
    try:
        response_classifier = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt_classifier}],
            model="qwen/qwen3.8-27b",
            temperature=0.1,
            max_tokens=20
        ).choices[0].message.content
        sentimen, topik = response_classifier.split('|')
    except:
        sentimen, topik = "Netral", "Umum"

    maks_iterasi = 2
    iterasi = 0
    draf_final = ""
    status_critic = "Disetujui otomatis"

    # Loop Evaluasi Agen Critic & Responder
    while iterasi < maks_iterasi:
        # 2. Agen Responder: Buat Draf
        prompt_responder = f"Buat 1 paragraf balasan untuk pelanggan restoran yang menulis: '{review_text}'. Sentimen: {sentimen.strip()}. Buat balasan yang sopan, empatik, tidak kaku. Jangan beri basa-basi di awal, langsung balasannya."
        draf_balasan = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt_responder}],
            model="qwen/qwen3.8-27b",
            temperature=0.6,
            max_tokens=150
        ).choices[0].message.content

        # 3. Agen Critic: Quality Control
        prompt_critic = f"Evaluasi draf balasan admin ini: '{draf_balasan}'. Apakah terkesan defensif, ngeyel, atau seperti robot? Jawab HANYA 'LULUS' jika bagus, atau 'TOLAK: [alasan singkat]' jika buruk."
        evaluasi = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt_critic}],
            model="qwen/qwen3.8-27b",
            temperature=0.1,
            max_tokens=30
        ).choices[0].message.content

        if "LULUS" in evaluasi.upper():
            draf_final = draf_balasan
            if iterasi > 0:
                status_critic = "Disetujui setelah Revisi"
            break
        else:
            iterasi += 1
            status_critic = evaluasi
            draf_final = draf_balasan 

    return sentimen.strip(), topik.strip(), draf_final.strip(), status_critic.strip()

@app.route('/')
def index():
    # Mengambil semua data dari SQLite untuk ditampilkan
    reviews = Review.query.all()
    return render_template('index.html', reviews=reviews)

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return redirect(request.url)
    file = request.files['file']
    
    if file and file.filename.endswith('.csv'):
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file.filename))
        file.save(filepath)
        
        df = pd.read_csv(filepath)
        
        # Batasi 3 baris pertama agar API gratis tidak rate limit saat demo
        for index, row in df.head(3).iterrows():
            ulasan_teks = str(row.get('ulasan', row.iloc[0])) 
            sentimen, topik, draf, status = run_agentic_loop(ulasan_teks)
            
            new_review = Review(
                teks_asli=ulasan_teks, sentimen=sentimen, 
                topik=topik, draf_balasan=draf, status_revisi=status
            )
            db.session.add(new_review)
        
        db.session.commit()
        return redirect(url_for('index'))
        
    return "Format file wajib .csv"


@app.route('/delete/<int:id>', methods=['POST'])
def delete_review(id):
    review_to_delete = Review.query.get_or_404(id)
    try:
        db.session.delete(review_to_delete)
        db.session.commit()
        return redirect(url_for('index'))
    except:
        return "Terjadi kesalahan saat menghapus data."

    
if __name__ == '__main__':
    app.run(debug=True)