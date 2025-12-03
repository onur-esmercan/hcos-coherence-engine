import os
import json
import time
import math
import re
import google.generativeai as genai
from dotenv import load_dotenv
import logging

# --- AYARLAR ---
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=API_KEY)

INPUT_FOLDER = "./inputs"
OUTPUT_FOLDER = "./outputs"
FINAL_REPORT_FOLDER = "./final_report"

# Model (1.5 Pro - Uzun bağlam için en iyisi)
MODEL_NAME = "gemini-1.5-pro-latest" 

generation_config = {
    "temperature": 0.1, # Daha tutarlı olması için düşürdük
    "top_p": 0.95,
    "max_output_tokens": 8192,
    "response_mime_type": "application/json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("orchestrator_v2.log"),
        logging.StreamHandler()
    ]
)

# --- PROMPTLAR ---
MINER_PROMPT = '''
GÖREV: Sen "Content Miner V3"sün. Uzun ve karmaşık konuşma loglarını okuyup yapılandırılmış veri çıkarırsın.
KRİTİK: Eğer girdi [PART X/Y] etiketi taşıyorsa, bu bir bütünün parçasıdır. Önceki parçalardan bağımsız olarak sadece elindeki metindeki veriyi çıkar, birleştirme işini yönetici yapacak.
ÇIKTI JSON ŞEMASI:{ "extracted_items": [ { "core_idea": "...", "problem": "...", "solution": "...", "code_snippets": ["..."], "user_mood": "..." } ], "trace_log": ["Sayfa 10-15 tarandı", "Kod bloğu tespit edildi"] }
KURAL: Kod bloklarında değişiklik varsa (önceki versiyona göre) bunu "code_evolution_note" olarak ekle. 
'''
VALIDATOR_PROMPT = '''
GÖREV: Sen "Tech Validator V3"sün. Miner'dan gelen ham fikir envanterini teknik olarak denetle.ANALİZ:
Kod-Fikir Tutarlılığı: Fikir "modüler" diyor ama kod "monolitik" ise bunu "High Tech Debt" olarak işaretle.
Kod Kalitesi: Kod blokları modern standartlara (SOLID, Async vb.) uygun mu?
ÇIKTI: Gelen JSON'a "tech_evaluation": { "integrity_score": 1-100, "refactoring_needs": "...", "architecture_type": "Microservices/Monolith/Serverless" } ekle.
'''
STRATEGIST_PROMPT = '''
GÖREV: Sen "GTM Strategist V3"sün. Teknik veriyi al ve [CURRENT_DATE] pazar dinamiklerine göre değerlendir.ANALİZ:
Bu fikir şu anki AI Agent trendlerine (AutoGPT, LangGraph, CrewAI) rakip mi yoksa tamamlayıcı mı?
Pazarın bu ürüne şu an ihtiyacı var mı? (Market Readiness).ÇIKTI: Gelen JSON'a "market_intelligence": { "market_fit_score": 1-100, "competitors": [...], "pivot_suggestion": "..." } ekle.
'''
CLUSTERING_PROMPT = '''
GÖREV: Verilen analiz özetlerini oku ve bunları "Ürün/Konsept" bazında grupla.
ÇIKTI: { "clusters": { "HCOS_Ecosystem": ["file1.json", "file5.json"], "DNA_Architecture": ["file2.json"] ... } }
'''
ARCHITECT_PROMPT = '''
GÖREV: Sen "Grand Architect"sin. Sana bir konu kümesi (Cluster) ve o kümeye ait tüm analiz dosyaları verildi.
HEDEF: Bu küme içindeki tüm parça fikirleri birleştirip TEK BİR TUTARLI ÜRÜN MİMARİSİ ortaya çıkarmak.
YÖNTEM:Çelişkileri gider: Eğer V1.0 ve V3.0 arasında çelişki varsa, V3.0'ı (veya Validator puanı en yüksek olanı) kabul et.
Nihai Ürünü Tanımla: Bu küme aslında neyi inşa etmeye çalışıyordu?ÇIKTI: Detaylı bir Markdown Raporu ve JSON Mimari Tanımı.
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
    """Parçalı miner sonuçlarını tek bir JSON'da birleştirir."""
    master_json = {
        "packet_id": results_list[0].get("packet_id"),
        "extracted_items": [],
        "trace_log": []
    }
    for res in results_list:
        if "extracted_items" in res:
            master_json["extracted_items"].extend(res["extracted_items"])
        if "trace_log" in res:
            master_json["trace_log"].extend(res["trace_log"])
    return master_json

def run_agent_with_retry(agent_name, prompt, content, retries=3):
    model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=prompt, generation_config=generation_config)
    
    for attempt in range(retries):
        try:
            response = model.generate_content(content)
            return json.loads(response.text)
        except Exception as e:
            logging.warning(f"{agent_name} Hatası (Deneme {attempt + 1}/{retries}): {e}")
            time.sleep(5) # Bekle ve tekrar dene
        logging.error(f"{agent_name} tamamen başarısız oldu, içerik işlenemedi.")        
    return None
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
    with open(filepath, 'r', encoding='utf-8') as f:
        raw_text = f.read()

    # 1. ADIM: MINER (Bölme Mantığı ile)
    # Eğer dosya çok büyükse veya önceki denemede hata verdiyse bölerek işle
    chunks = smart_split(raw_text)
    miner_results = []
    
    print(f"  -> Miner çalışıyor ({len(chunks)} parça)...")
    for i, chunk in enumerate(chunks):
        # Parça bağlamı ekle
        context_header = f"[PART {i+1}/{len(chunks)}] "
        res = run_agent_with_retry("Miner", MINER_PROMPT, context_header + chunk)
        if res: miner_results.append(res)
    
    if not miner_results:
        print(f"  XX {filename} Miner aşamasında başarısız oldu.")
        return
        
    # Sonuçları Birleştir
    full_miner_data = merge_miner_results(miner_results)

    # 2. ADIM: VALIDATOR (Tek seferde tüm veriyi değerlendirir)
    print(f"  -> Validator çalışıyor...")
    validator_data = run_agent_with_retry("Validator", VALIDATOR_PROMPT, json.dumps(full_miner_data))
    if validator_data: full_miner_data.update(validator_data)

    # 3. ADIM: STRATEGIST
    print(f"  -> Strategist çalışıyor...")
    strategist_data = run_agent_with_retry("Strategist", STRATEGIST_PROMPT, json.dumps(full_miner_data))
    if strategist_data: full_miner_data.update(strategist_data)

    # KAYDET
    output_path = os.path.join(OUTPUT_FOLDER, filename.replace('.md', '.json').replace('.txt', '.json'))
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(full_miner_data, f, indent=2, ensure_ascii=False)
    print(f"  OK. Kaydedildi: {output_path}")

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
