"""
GitHub Actions에서 매일 자동 실행되어 docs/index.html 을 갱신하는 스크립트.
카카오 REST API 키는 환경변수 KAKAO_REST_API_KEY 로 전달받습니다 (GitHub Secrets에 등록).
"""

import os
import csv
import io
import math
import json
import datetime
import requests

# ── 설정 ──────────────────────────────────────────
ORIGIN_ADDRESS = "서울 중구 남대문로 125"
RADIUS_M = 2000            # 최종 반경 (미터)
RING_SPACING_M = 700       # 그리드 촘촘함 (작을수록 45개 제한을 더 잘 우회하지만 API 호출 수가 늘어남)
CATEGORY_CODE = "FD6"      # FD6 = 음식점
OUTPUT_PATH = "docs/index.html"
OVERRIDES_PATH = "cuisine_overrides.json"  # 팀 전체에 영구 반영할 분류 수정 목록 (저장소 루트에 직접 만들면 됨)

# ── 팀 자체 별점 (구글 폼 + 구글 시트 연동) ──────────
# 구글 시트를 "파일 > 공유 > 웹에 게시 > CSV"로 공개한 뒤 그 링크를 아래에 붙여넣으세요.
# 비워두면(기본값) 별점 기능은 자동으로 비활성화됩니다.
RATINGS_CSV_URL = ""

# 폼 응답 미리 채우기 링크. 구글 폼에서 [응답 미리보기 → 사전 채우기 링크 받기]로 얻은 뒤,
# 식당 이름이 들어가는 자리를 반드시 {name} 으로 바꿔서 넣으세요.
# 예: "https://docs.google.com/forms/d/e/1FAI.../viewform?usp=pp_url&entry.123456789={name}"
RATING_FORM_LINK_TEMPLATE = ""
# ────────────────────────────────────────────────

KAKAO_REST_API_KEY = os.environ["KAKAO_REST_API_KEY"]
HEADERS = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}

CUISINE_ORDER = ["한식", "중식", "양식", "일식", "분식", "기타"]


def classify_cuisine(category_name: str) -> str:
    """카카오 category_name(예: '음식점 > 한식 > 육류,고기')을 보고 대분류를 매긴다."""
    if "한식" in category_name:
        return "한식"
    if "중식" in category_name or "중국" in category_name:
        return "중식"
    if "일식" in category_name or "일본" in category_name:
        return "일식"
    if "분식" in category_name:
        return "분식"
    if "양식" in category_name:  # '경양식'도 '양식' 글자를 포함하므로 여기로 함께 분류됨
        return "양식"
    return "기타"


def get_origin_coords(address: str):
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    res = requests.get(url, headers=HEADERS, params={"query": address})
    res.raise_for_status()
    docs = res.json().get("documents", [])
    if not docs:
        raise ValueError(f"주소를 찾을 수 없습니다: {address}")
    d = docs[0]
    return float(d["y"]), float(d["x"])  # lat, lng


def haversine(lat1, lng1, lat2, lng2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def offset_point(lat, lng, dx_m, dy_m):
    dlat = dy_m / 111320
    dlng = dx_m / (111320 * math.cos(math.radians(lat)))
    return lat + dlat, lng + dlng


def build_grid(lat, lng, radius, spacing):
    points = [(lat, lng)]
    ring = 1
    while ring * spacing <= radius:
        r = ring * spacing
        for angle_deg in range(0, 360, 45):
            angle = math.radians(angle_deg)
            dx = r * math.sin(angle)
            dy = r * math.cos(angle)
            points.append(offset_point(lat, lng, dx, dy))
        ring += 1
    return points


def search_category(lat, lng, radius):
    url = "https://dapi.kakao.com/v2/local/search/category.json"
    results = []
    page = 1
    while True:
        params = {
            "category_group_code": CATEGORY_CODE,
            "x": lng, "y": lat,
            "radius": radius,
            "page": page, "size": 15,
            "sort": "distance",
        }
        res = requests.get(url, headers=HEADERS, params=params)
        res.raise_for_status()
        data = res.json()
        results.extend(data["documents"])
        if data["meta"]["is_end"] or page >= 3:
            break
        page += 1
    return results


def collect_all(origin_lat, origin_lng, radius, spacing):
    grid_points = build_grid(origin_lat, origin_lng, radius, spacing)
    seen = {}
    for glat, glng in grid_points:
        try:
            docs = search_category(glat, glng, spacing)
        except requests.HTTPError:
            continue
        for d in docs:
            pid = d["id"]
            if pid in seen:
                continue
            plat, plng = float(d["y"]), float(d["x"])
            dist = haversine(origin_lat, origin_lng, plat, plng)
            if dist > radius:
                continue
            if not d.get("phone"):
                continue
            seen[pid] = {
                "name": d["place_name"],
                "address": d["road_address_name"] or d["address_name"],
                "distance": int(dist),
                "phone": d["phone"],
                "url": d["place_url"],
                "cuisine": classify_cuisine(d.get("category_name", "")),
            }
    return sorted(seen.values(), key=lambda x: x["distance"])


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>오늘 점심 뽑기</title>
<style>
  :root{{ --bg:#f2fbf9; --ink:#122b27; --accent:#14b8a6; --line:#cdeee7; --card:#ffffff; }}
  *{{box-sizing:border-box;}}
  body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;background:var(--bg);color:var(--ink);padding:24px 16px 60px;}}
  .wrap{{max-width:520px;margin:0 auto;}}
  h1{{font-size:22px;margin:0 0 4px;}}
  .sub{{color:#7a6f5f;font-size:13px;margin-bottom:20px;}}
  .controls{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:16px;}}
  .row{{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:14px;}}
  .row:last-child{{margin-bottom:0;}}
  input[type=range]{{width:150px;}}
  .val{{font-weight:600;color:var(--accent);min-width:50px;text-align:right;}}
  .chips{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px;}}
  .chip{{padding:8px 14px;border-radius:999px;border:1px solid var(--line);background:var(--card);font-size:13px;cursor:pointer;}}
  .chip.active{{background:var(--accent);color:#fff;border-color:var(--accent);}}
  button#pickBtn{{width:100%;padding:16px;font-size:17px;font-weight:700;color:#fff;background:var(--accent);border:none;border-radius:14px;cursor:pointer;margin-bottom:12px;}}
  button#resetBtn{{width:100%;padding:10px;font-size:13px;color:#8a8070;background:transparent;border:1px solid var(--line);border-radius:10px;cursor:pointer;margin-bottom:20px;}}
  .result{{background:var(--card);border:2px solid var(--accent);border-radius:16px;padding:20px;margin-bottom:20px;display:none;}}
  .result.show{{display:block;}}
  .result .name{{font-size:20px;font-weight:800;margin-bottom:6px;}}
  .result .meta{{font-size:13px;color:#6b6153;line-height:1.6;}}
  .result a{{color:var(--accent);}}
  .count{{font-size:13px;color:#7a6f5f;margin-bottom:8px;}}
  .item{{padding:12px 0;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px;font-size:14px;}}
  .item .m{{color:#8a8070;font-size:12px;white-space:nowrap;}}
  .item.recent{{opacity:0.4;}}
  .tag{{cursor:pointer;text-decoration:underline dotted;padding:1px 4px;border-radius:6px;}}
  .tag:active{{background:var(--line);}}
  .hint{{font-size:11px;color:#8a8070;margin:-12px 0 16px;}}
  .stars{{color:#e0a300;}}
  .norating{{color:#b7b0a4;}}
  .ratingLink{{display:inline-block;margin-top:6px;font-size:13px;color:var(--accent);}}
  button#exportBtn{{width:100%;padding:10px;font-size:13px;color:var(--accent);background:transparent;border:1px dashed var(--accent);border-radius:10px;cursor:pointer;margin-bottom:12px;}}
  .exportBox{{display:none;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:20px;}}
  .exportBox.show{{display:block;}}
  .exportHint{{font-size:12px;color:#6b6153;margin-bottom:8px;line-height:1.5;}}
  .exportHint code{{background:var(--bg);padding:1px 4px;border-radius:4px;}}
  #exportText{{width:100%;height:100px;font-size:12px;font-family:monospace;border:1px solid var(--line);border-radius:8px;padding:8px;}}
  .empty{{padding:20px;text-align:center;color:#8a8070;font-size:14px;}}
</style>
</head>
<body>
<div class="wrap">
  <h1>🍚 오늘 점심 뭐 먹지?</h1>
  <div class="sub">{origin_address} 기준 · 반경 {radius}m · 카카오맵 데이터 · 자동 갱신: {generated_at}</div>

  <div class="controls">
    <div class="row">
      <span>반경</span>
      <span><input type="range" id="radius" min="100" max="{radius}" step="50" value="{radius}"><span class="val" id="radiusVal"></span></span>
    </div>
    <div class="row">
      <span id="minRatingLabel">⭐ 별점 전체</span>
      <input type="range" id="minRating" min="0" max="5" step="0.5" value="0">
    </div>
  </div>

  <div class="chips" id="cuisineChips"></div>

  <button id="pickBtn">🎲 랜덤으로 점심 뽑기</button>
  <button id="resetBtn">최근 뽑힌 기록 초기화</button>
  <button id="exportBtn">✏️ 내 분류 수정사항 팀 전체에 반영하기</button>

  <div class="exportBox" id="exportBox">
    <div class="exportHint">
      아래 내용을 전체 선택해서 복사한 뒤, 저장소의 <code>cuisine_overrides.json</code> 파일을 열어
      (없으면 새로 만들어서) 그대로 붙여넣고 저장하세요. 다음 자동 갱신부터 모두에게 반영돼요.
    </div>
    <textarea id="exportText" readonly onclick="this.select()"></textarea>
  </div>

  <div class="result" id="result">
    <div class="name" id="rName"></div>
    <div class="meta" id="rMeta"></div>
  </div>

  <div class="hint">💡 분류가 틀렸으면 목록에서 [분류] 글자를 탭하면 바로 바뀌어요 (이 폰에서만 적용).</div>
  <div class="count" id="countLabel"></div>
  <div id="list"></div>
</div>

<script>
const restaurants = {data_json};
const CUISINES = {cuisine_order_json};
const RECENT_KEY = 'lunchpick_recent_v1';
const OVERRIDE_KEY = 'lunchpick_overrides_v1';
const RECENT_MAX = 5;

let selectedCuisine = '전체';

function getOverrides(){{ try {{ return JSON.parse(localStorage.getItem(OVERRIDE_KEY) || '{{}}'); }} catch(e) {{ return {{}}; }} }}
function setOverride(name, cuisine){{
  const ov = getOverrides();
  ov[name] = cuisine;
  localStorage.setItem(OVERRIDE_KEY, JSON.stringify(ov));
}}
function effectiveCuisine(r){{
  const ov = getOverrides();
  return ov[r.name] || r.cuisine;
}}
function cycleCuisine(current){{
  const idx = CUISINES.indexOf(current);
  return CUISINES[(idx + 1) % CUISINES.length];
}}

const radiusEl = document.getElementById('radius');
const radiusVal = document.getElementById('radiusVal');
const minRatingEl = document.getElementById('minRating');
const minRatingLabel = document.getElementById('minRatingLabel');
const listEl = document.getElementById('list');
const countLabel = document.getElementById('countLabel');
const resultEl = document.getElementById('result');
const chipsEl = document.getElementById('cuisineChips');
const RATING_FORM_LINK_TEMPLATE = {rating_form_link_json};

function getRecent(){{
  try {{ return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); }} catch(e) {{ return []; }}
}}
function addRecent(name){{
  const recent = getRecent();
  recent.push(name);
  while(recent.length > RECENT_MAX) recent.shift();
  localStorage.setItem(RECENT_KEY, JSON.stringify(recent));
}}

function buildChips(){{
  const options = ['전체', ...CUISINES];
  chipsEl.innerHTML = '';
  options.forEach(c => {{
    const chip = document.createElement('div');
    chip.className = 'chip' + (c === selectedCuisine ? ' active' : '');
    chip.textContent = c;
    chip.addEventListener('click', () => {{ selectedCuisine = c; buildChips(); render(); }});
    chipsEl.appendChild(chip);
  }});
}}

function starsText(rating, count){{
  if(rating === null || rating === undefined || count === 0){{
    return '<span class="norating">☆ 아직 평가 없음</span>';
  }}
  const full = Math.round(rating);
  return `<span class="stars">${{'★'.repeat(full)}}${{'☆'.repeat(5-full)}}</span> ${{rating.toFixed(1)}} (${{count}}명)`;
}}

function buildRatingLink(name){{
  if(!RATING_FORM_LINK_TEMPLATE) return null;
  return RATING_FORM_LINK_TEMPLATE.replace('{{name}}', encodeURIComponent(name));
}}

function currentFiltered(){{
  const radius = +radiusEl.value;
  const minRating = +minRatingEl.value;
  return restaurants
    .filter(r => r.distance <= radius)
    .filter(r => selectedCuisine === '전체' || effectiveCuisine(r) === selectedCuisine)
    .filter(r => minRating === 0 || (r.team_rating ?? 0) >= minRating)
    .sort((a,b) => a.distance - b.distance);
}}

function render(){{
  radiusVal.textContent = radiusEl.value + 'm';
  const minRatingNow = +minRatingEl.value;
  minRatingLabel.textContent = minRatingNow === 0 ? '⭐ 별점 전체' : `⭐ 별점 ${{minRatingNow.toFixed(1)}}점 이상`;
  const filtered = currentFiltered();
  const recent = getRecent();
  countLabel.textContent = `${{selectedCuisine}} · 반경 내 ${{filtered.length}}곳`;
  listEl.innerHTML = '';
  if(filtered.length === 0){{
    listEl.innerHTML = '<div class="empty">조건에 맞는 곳이 없어요.</div>';
    return;
  }}
  filtered.forEach(r => {{
    const cuisine = effectiveCuisine(r);
    const div = document.createElement('div');
    div.className = 'item' + (recent.includes(r.name) ? ' recent' : '');
    div.innerHTML = `<span>${{r.name}} <span class="m tag" data-name="${{r.name}}" data-cur="${{cuisine}}">[${{cuisine}}]</span><br><span class="m">${{starsText(r.team_rating, r.team_rating_count)}}</span></span><span class="m">${{r.distance}}m</span>`;
    listEl.appendChild(div);
  }});
  listEl.querySelectorAll('.tag').forEach(el => {{
    el.addEventListener('click', (e) => {{
      e.stopPropagation();
      const name = el.getAttribute('data-name');
      const cur = el.getAttribute('data-cur');
      const next = cycleCuisine(cur);
      setOverride(name, next);
      render();
    }});
  }});
}}

radiusEl.addEventListener('input', render);
minRatingEl.addEventListener('input', render);

document.getElementById('resetBtn').addEventListener('click', () => {{
  localStorage.removeItem(RECENT_KEY);
  render();
}});

document.getElementById('exportBtn').addEventListener('click', () => {{
  const box = document.getElementById('exportBox');
  const overrides = getOverrides();
  if(Object.keys(overrides).length === 0){{
    alert('아직 이 폰에서 수정한 분류가 없어요. 목록에서 [분류] 글자를 먼저 탭해서 고쳐보세요.');
    return;
  }}
  document.getElementById('exportText').value = JSON.stringify(overrides, null, 2);
  box.classList.toggle('show');
}});

document.getElementById('pickBtn').addEventListener('click', () => {{
  const filtered = currentFiltered();
  if(filtered.length === 0){{ alert('조건에 맞는 곳이 없어요. 분류나 별점, 반경을 조정해보세요!'); return; }}
  const recent = getRecent();
  let pool = filtered.filter(r => !recent.includes(r.name));
  if(pool.length === 0) pool = filtered;
  const pick = pool[Math.floor(Math.random() * pool.length)];
  addRecent(pick.name);
  const ratingLink = buildRatingLink(pick.name);
  document.getElementById('rName').textContent = '오늘 점심은 👉 ' + pick.name;
  document.getElementById('rMeta').innerHTML =
    `[${{effectiveCuisine(pick)}}] · ${{pick.distance}}m · ${{pick.address}}<br>` +
    `📞 ${{pick.phone}}<br>` +
    `${{starsText(pick.team_rating, pick.team_rating_count)}}<br>` +
    `<a href="${{pick.url}}" target="_blank">카카오맵에서 리뷰 보기 →</a>` +
    (ratingLink ? `<br><a class="ratingLink" href="${{ratingLink}}" target="_blank">⭐ 다녀와서 별점 남기기 →</a>` : '');
  resultEl.classList.add('show');
  render();
}});

buildChips();
render();
</script>
</body>
</html>
"""


def load_overrides():
    """cuisine_overrides.json 이 저장소에 있으면 읽어서 {가게이름: 분류} 형태로 반환.
    파일이 없으면 빈 dict (에러 없이 그냥 넘어감)."""
    if not os.path.exists(OVERRIDES_PATH):
        return {}
    try:
        with open(OVERRIDES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_ratings_csv(text: str):
    """구글 시트에서 받은 CSV 텍스트를 파싱해 {가게이름: [평균별점, 응답수]} 형태로 반환.
    열 순서에 상관없이 헤더에 '이름'이 들어간 열과 '별점'이 들어간 열을 찾아서 사용한다."""
    reader = list(csv.reader(io.StringIO(text)))
    if not reader:
        return {}
    header = reader[0]
    name_col = next((i for i, h in enumerate(header) if "이름" in h), None)
    score_col = next((i for i, h in enumerate(header) if "별점" in h), None)
    if name_col is None or score_col is None:
        return {}

    tally = {}  # name -> [sum, count]
    for row in reader[1:]:
        if len(row) <= max(name_col, score_col):
            continue
        name = row[name_col].strip()
        try:
            score = float(row[score_col])
        except ValueError:
            continue
        if not name:
            continue
        if name not in tally:
            tally[name] = [0.0, 0]
        tally[name][0] += score
        tally[name][1] += 1

    return {
        name: {"avg": round(total / count, 1), "count": count}
        for name, (total, count) in tally.items()
    }


def fetch_team_ratings():
    if not RATINGS_CSV_URL:
        return {}
    try:
        res = requests.get(RATINGS_CSV_URL, timeout=15)
        res.raise_for_status()
        return parse_ratings_csv(res.text)
    except Exception:
        return {}


def main():
    lat, lng = get_origin_coords(ORIGIN_ADDRESS)
    places = collect_all(lat, lng, RADIUS_M, RING_SPACING_M)

    overrides = load_overrides()
    for p in places:
        if p["name"] in overrides:
            p["cuisine"] = overrides[p["name"]]

    ratings = fetch_team_ratings()
    for p in places:
        r = ratings.get(p["name"])
        p["team_rating"] = r["avg"] if r else None
        p["team_rating_count"] = r["count"] if r else 0

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    html = HTML_TEMPLATE.format(
        origin_address=ORIGIN_ADDRESS,
        radius=RADIUS_M,
        generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        data_json=json.dumps(places, ensure_ascii=False),
        cuisine_order_json=json.dumps(CUISINE_ORDER, ensure_ascii=False),
        rating_form_link_json=json.dumps(RATING_FORM_LINK_TEMPLATE, ensure_ascii=False),
    )
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"완료: {len(places)}곳 -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
