
import os
import json
import time
import math
import re
import logging
import google.generativeai as genai
from dotenv import load_dotenv

# --- AYARLAR ---
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=API_KEY)

INPUT_FOLDER = "./inputs"
OUTPUT_FOLDER = "./outputs"
DEBUG_FOLDER = "./outputs/debug_logs" # Yeni: Ara kayıtlar buraya
FINAL_REPORT_FOLDER = "./final_report"

MODEL_NAME = "gemini-2.5-pro" # 2.0 veya 1.5 Pro (Erişimin olanı seç)

generation_config = {
    "temperature": 0.1,
    "top_p": 0.95,
    "max_output_tokens": 8192,
    "response_mime_type": "application/json",
}

# Klasörleri oluştur
for folder in [INPUT_FOLDER, OUTPUT_FOLDER, DEBUG_FOLDER, FINAL_REPORT_FOLDER]:
    if not os.path.exists(folder):
        os.makedirs(folder)

# --- LOGGING AYARLARI (Encoding Hatası Çözümü) ---
# Windows'ta Türkçe karakter hatasını önlemek için encoding='utf-8' şart
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("orchestrator.log", encoding='utf-8'), # DÜZELTME BURADA
        logging.StreamHandler()
    ]
)

# --- PROMPTLAR (Aynı Kalabilir veya Sertleştirilebilir) ---
MINER_PROMPT = '''
GÖREV: Sen "Content Miner V3"sün. Uzun ve karmaşık konuşma loglarını okuyup yapılandırılmış veri çıkarırsın.
KRİTİK: Eğer girdi [PART X/Y] etiketi taşıyorsa, bu bir bütünün parçasıdır. Önceki parçalardan bağımsız olarak sadece elindeki metindeki verilerin hepsini çıkar, birleştirme işini yönetici yapacak.
KURAL: Kod bloklarında değişiklik varsa (önceki versiyona göre) bunu "code_evolution_note" olarak ekle. 
ÇIKTI JSON ŞEMASI:{ "extracted_items": [ { "core_idea": "...", "problem": "...", "solution": "...", "code_snippets": ["..."], "user_mood": "..." } ], "trace_log": ["..."] }
'''
VALIDATOR_PROMPT = '''
GÖREV: Sen "Tech Validator V3"sün. Miner'dan gelen ham fikir envanterini Kod/yayın vs teknik olarak denetle.
ANALİZ: Kod-Fikir Tutarlılığı: Örneğin Fikir "modüler" diyor ama kod "monolitik" ise bunu "High Tech Debt" olarak belirt, ...
Kod Kalitesi: Kod blokları modern standartlara (SOLID, Async vb.) uygun mu?
ÇIKTI: Gelen JSON'a "tech_evaluation": { "integrity_score": 1-100, "refactoring_needs": "...", "architecture_type": "Microservices/Monolith/Serverless/..." } ekle.
'''
STRATEGIST_PROMPT = '''
GÖREV: Sen "GTM Strategist V3"sün. Teknik veriyi al ve [CURRENT_DATE] pazar dinamiklerine göre değerlendir.
ANALİZ: Bu fikir şu anki market trendlerin içerisinde nasıl konumlanır? Rakip mi, tamamlayıcı mı, pazarına göre zayıf mı?
Pazarın bu ürüne şu an ihtiyacı var mı? (Market Readiness).
ÇIKTI: Gelen JSON'a "market_intelligence": { "market_fit_score": 1-100, "competitors": [...], "pivot_suggestion": "..." } ekle.
'''
CLUSTERING_PROMPT = '''
GÖREV: Sen "Tech Validator V3"sün. Verilen analiz özetlerini oku ve bunları "Ürün/Konsept" bazında grupla.
ÇIKTI: { "clusters": { "...": ["xxx.json", "xxx.html",...], "...": ["....json"] ... } }
'''
ARCHITECT_PROMPT = '''
GÖREV: Sen "Grand Architect"sin. Sana bir konu kümesi (Cluster) ve o kümeye ait tüm analiz dosyaları verildi.
HEDEF: Bu küme içindeki tüm parça fikirleri birleştirip Ürünlerin "Tutarlı ve Bütünlüklü Mimarisini" kurmak ve "Fikirlerin Evrimini" haritalamak.
Nihai Ürünü Tanımla: Bu küme aslında neyi inşa etmeye çalışıyordu? Bu kümeden ne çıkarılabilir?
ÇIKTI: Detaylı bir Markdown Raporu ve Eksikleri giderilmiş kod, servis,... ve whitepaper, GTM strategy, .... çıkar.
'''

# --- YARDIMCI FONKSİYONLAR ---

def smart_split(text, max_chars=30000):
    """
    Metni bölmek gerekirse, kod bloklarını (```) kırmadan 
    en uygun paragraf sonundan böler.
    """
    if len(text) <= max_chars:
        return [text]
    
    chunks = []
    while len(text) > max_chars:
        # Güvenli bir kesme noktası bul (yaklaşık ortalarda ama paragraf sonunda)
        split_candidate = text[:max_chars]
        
        # Kod bloğu kontrolü (Tek sayıda backtick varsa kod bloğu içindeyiz demektir)
        backtick_count = split_candidate.count("```")
        if backtick_count % 2 != 0:
            # Kod bloğu içindeyiz, geriye doğru ilk ``` arayalım
            last_code_block = split_candidate.rfind("```")
            split_point = last_code_block # Kod bloğundan hemen önce kes
        else:
            # Paragraf sonu veya başlık öncesi bul
            last_newline = split_candidate.rfind("\n\n")
            split_point = last_newline if last_newline != -1 else max_chars

        chunks.append(text[:split_point])
        text = text[split_point:]
    
    if text: chunks.append(text)
    return chunks

def merge_miner_results(results_list):
    """Parçalı sonuçları birleştirir."""
    master_json = {"packet_id": "MERGED", "extracted_items": [], "trace_log": []}
    for res in results_list:
        if isinstance(res, dict):
            master_json["extracted_items"].extend(res.get("extracted_items", []))
            master_json["trace_log"].extend(res.get("trace_log", []))
    return master_json

def run_agent_with_retry(agent_name, prompt, content, retries=3, use_search=False):
    tools = [{'google_search': {}}] if use_search else None
    
    model = genai.GenerativeModel(
        model_name=MODEL_NAME, system_instruction=prompt, generation_config=generation_config)
    for attempt in range(retries):
        try:
            response = model.generate_content(content)
            text = response.text.strip()
            
            # JSON Temizliği
            if text.startswith("```json"): text = text[7:]
            if text.endswith("```"): text = text[:-3]
            
            return json.loads(text)
            
        except Exception as e:
            logging.warning(f"{agent_name} Hatası (Deneme {attempt + 1}/{retries}): {e}")
            time.sleep(10) # Biraz daha uzun bekle
            
    logging.error(f"{agent_name} tamamen başarısız oldu.")        
    return None

def save_debug_json(data, filename, stage):
    """Ara aşama verisini kaydeder (İzlenebilirlik için)."""
    debug_name = f"{filename}_{stage}.json"
    path = os.path.join(DEBUG_FOLDER, debug_name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logging.info(f"  -> Ara Kayıt: {debug_name}")
def save_json(data, filepath):
"""JSON verisini dosyaya kaydeder."""
with open(filepath, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

def load_json(filepath):
"""JSON dosyasını yükler."""
with open(filepath, 'r', encoding='utf-8') as f:
    return json.load(f)
# --- ANA İŞLEM FONKSİYONU ---

def process_file_pipeline(filepath):
    filename = os.path.basename(filepath)
    filename_no_ext = os.path.splitext(filename)[0] # Uzantısız isim
    
    output_path = os.path.join(OUTPUT_FOLDER, filename.replace('.md', '.json').replace('.txt', '.json'))
    if os.path.exists(output_path):
        logging.info(f"  -> {filename} zaten işlenmiş, geçiliyor.")
        return

    with open(filepath, 'r', encoding='utf-8') as f: raw_text = f.read()

    # 1. ADIM: MINER
    chunks = smart_split(raw_text)
    miner_results = []
    
    logging.info(f"--- {filename} İŞLENİYOR ---")
    logging.info(f"  -> Miner ({len(chunks)} parça)...")
    
    for i, chunk in enumerate(chunks):
        res = run_agent_with_retry("Miner", MINER_PROMPT, f"[PART {i+1}/{len(chunks)}] \n {chunk}")
        if res: miner_results.append(res)
    
    if not miner_results:
        logging.error(f"  XX {filename} Miner başarısız.")
        return
        
    full_miner_data = merge_miner_results(miner_results)
    save_debug_json(full_miner_data, filename_no_ext, "01_Miner") # ARA KAYIT

    # 2. ADIM: VALIDATOR
    logging.info(f"  -> Validator...")
    validator_data = run_agent_with_retry("Validator", VALIDATOR_PROMPT, json.dumps(full_miner_data))
    
    if validator_data:
        # Liste dönerse ilk elemanı al (Hata önleyici)
        if isinstance(validator_data, list) and len(validator_data) > 0:
             full_miner_data.update(validator_data[0])
        elif isinstance(validator_data, dict):
             full_miner_data.update(validator_data)
             
        save_debug_json(full_miner_data, filename_no_ext, "02_Validator") # ARA KAYIT
    else:
        logging.warning("  !! Validator veri döndürmedi, Miner çıktısıyla devam ediliyor.")

    # 3. ADIM: STRATEGIST
    logging.info(f"  -> Strategist...")
    strategist_data = run_agent_with_retry("Strategist", STRATEGIST_PROMPT, json.dumps(full_miner_data), use_search=True)
    
    if strategist_data:
        if isinstance(strategist_data, list) and len(strategist_data) > 0:
             full_miner_data.update(strategist_data[0])
        elif isinstance(strategist_data, dict):
             full_miner_data.update(strategist_data)
             
        save_debug_json(full_miner_data, filename_no_ext, "03_Strategist") # ARA KAYIT
    else:
        logging.warning("  !! Strategist veri döndürmedi.")

    # NİHAİ KAYIT
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(full_miner_data, f, indent=2, ensure_ascii=False)
    logging.info(f"  ✓ {filename} tamamlandı.")

# --- SENTEZ FONKSİYONU ---
def run_synthesis():
    print("\n--- SENTEZ AŞAMASI BAŞLIYOR ---")
    if not os.path.exists(FINAL_REPORT_FOLDER): os.makedirs(FINAL_REPORT_FOLDER)
    
    all_jsons = []
    for f in os.listdir(OUTPUT_FOLDER):
        if f.endswith('.json'):
            with open(os.path.join(OUTPUT_FOLDER, f), 'r', encoding='utf-8') as file:
                all_jsons.append(json.load(file))
    
    # ÖNCE KÜMELEME (CLUSTERING)
    # Tüm JSON'ların sadece "Core Idea" başlıklarını çıkarıp modele veriyoruz.
    summaries = [{"file": f, "concepts": [i["core_idea"] for i in j.get("extracted_items", [])]} 
                 for f, j in zip(os.listdir(OUTPUT_FOLDER), all_jsons)]
    
    print("  -> Dosyalar konularına göre kümeleniyor...")
    clustering_result = run_agent_with_retry("Clusterer", CLUSTERING_PROMPT, json.dumps(summaries))
    
    if not clustering_result:
        print("  XX Kümeleme başarısız, düz sentez deneniyor.")
        clusters = {"All": [f for f in os.listdir(OUTPUT_FOLDER)]}
    else:
        clusters = clustering_result.get("clusters", {})

    # HER KÜME İÇİN ARCHITECT ÇALIŞTIR
    final_architecture = []
    
    for cluster_name, file_list in clusters.items():
        print(f"  -> Mimari Sentezleniyor: {cluster_name} ({len(file_list)} dosya)...")
        
        # Sadece bu kümeye ait JSON içeriklerini topla
        cluster_data = [j for f, j in zip(os.listdir(OUTPUT_FOLDER), all_jsons) if f in file_list]
        
        architect_result = run_agent_with_retry("Architect", ARCHITECT_PROMPT, json.dumps(cluster_data))
        if architect_result:
            final_architecture.append({"cluster": cluster_name, "analysis": architect_result})
            
            # Ara raporu kaydet
            with open(os.path.join(FINAL_REPORT_FOLDER, f"Architecture_{cluster_name}.json"), 'w', encoding='utf-8') as f:
                json.dump(architect_result, f, indent=2, ensure_ascii=False)

    print("\n--- SÜREÇ TAMAMLANDI ---")
    print(f"Raporlar {FINAL_REPORT_FOLDER} klasöründe.")

def main():
    if not os.path.exists(OUTPUT_FOLDER): os.makedirs(OUTPUT_FOLDER)
    
    files = [f for f in os.listdir(INPUT_FOLDER) if f.endswith(('.md', '.txt'))]
    print(f"--- BAŞLANGIÇ: {len(files)} Dosya ---")
    
    # Dosyaları İşle
    for file in files:
        print(f"\nDosya: {file}")
        process_file_pipeline(os.path.join(INPUT_FOLDER, file))
        
    # Hepsi bitince Sentezle
    run_synthesis()

if __name__ == "__main__":
    main()
    # Sadece process_file_pipeline çağrısını ve klasör taramayı içerir.
#    pass
