import os
import io
import csv
from flask import Flask, render_template, request, redirect, url_for, Response
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

app = Flask(__name__)

# Vercel HANYA mengizinkan penyimpanan file di folder /tmp
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////tmp/insightbite.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

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

    while iterasi < maks_iterasi:
        prompt_responder = f"Buat 1 paragraf balasan untuk pelanggan restoran yang menulis: '{review_text}'. Sentimen: {sentimen.strip()}. Buat balasan yang sopan, empatik, tidak kaku. Jangan beri basa-basi di awal, langsung balasannya."
        draf_balasan = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt_responder}],
            model="qwen/qwen3.8-27b",
            temperature=0.6,
            max_tokens=150
        ).choices[0].message.content

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

def run_manager_agent(reviews_list):
    if not reviews_list:
        return "Belum ada data ulasan untuk dirangkum."
    
    gabungan_ulasan = "\n".join([f"- {r.teks_asli} (Sentimen: {r.sentimen}, Topik: {r.topik})" for r in reviews_list])
    prompt_manager = f"""Berperanlah sebagai Manager Operasional Bisnis F&B. Berdasarkan data ulasan pelanggan berikut:
{gabungan_ulasan}

Buat rangkuman eksekutif bisnis singkat (2-3 kalimat) yang menyoroti tren operasional utama, titik masalah, dan poin evaluasi cabang."""
    try:
        rangkuman = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt_manager}],
            model="qwen/qwen3.8-27b",
            temperature=0.3,
            max_tokens=200
        ).choices[0].message.content
        return rangkuman.strip()
    except Exception:
        return "Gagal menghasilkan rangkuman eksekutif."

# --- ROUTES ---
@app.route('/')
def index():
    reviews = Review.query.all()
    insight_bisnis = run_manager_agent(reviews)
    return render_template('index.html', reviews=reviews, insight_bisnis=insight_bisnis)

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return redirect(request.url)
    file = request.files['file']
    
    if file and file.filename.endswith('.csv'):
        # Baca langsung dari memori untuk Vercel, tanpa simpan file fisik
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_reader = csv.reader(stream)
        next(csv_reader, None) # Lewati baris header CSV
        
        count = 0
        for row in csv_reader:
            if count >= 3: # Tetap batasi 3 baris
                break
            if row:
                ulasan_teks = row[0] # Mengambil kolom pertama
                sentimen, topik, draf, status = run_agentic_loop(ulasan_teks)
                
                new_review = Review(
                    teks_asli=ulasan_teks, sentimen=sentimen, 
                    topik=topik, draf_balasan=draf, status_revisi=status
                )
                db.session.add(new_review)
                count += 1
        
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
    except Exception:
        return "Terjadi kesalahan saat menghapus data."

@app.route('/download')
def download_csv():
    reviews = Review.query.all()
    if not reviews:
        return redirect(url_for('index'))
        
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Ulasan Asli', 'Sentimen', 'Topik', 'Draf Balasan', 'Status Critic'])
    
    for r in reviews:
        writer.writerow([r.teks_asli, r.sentimen, r.topik, r.draf_balasan, r.status_revisi])
    
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=insightbite_hasil_analisis.csv"}
    )

if __name__ == '__main__':
    app.run(debug=True)