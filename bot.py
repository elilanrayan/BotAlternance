import hashlib
import json
import os
import re
import time
import unicodedata
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
FT_CLIENT_ID = os.environ.get("FT_CLIENT_ID", "")          # Optionnel (France Travail)
FT_CLIENT_SECRET = os.environ.get("FT_CLIENT_SECRET", "")  # Optionnel (France Travail)

SEEN_FILE = "seen_offers.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Mots-clés Game Programming (profil ISART Digital)
GAME_TECH_KEYWORDS = [
    "unreal", "ue4", "ue5", "unity", "godot",
    "game programming", "game programmer", "gameplay", "engine programmer",
    "graphics programmer", "tools programmer", "ai programmer",
    "network programmer", "physics programmer", "audio programmer",
    "programmeur jeu", "developpeur jeu", "developpeur jeux",
    "jeu video", "jeux video", "gaming", "studio de jeu",
    "shader", "hlsl", "glsl", "directx", "vulkan", "opengl", "3d", "vr", "xr",
]

# Langages ciblés (combinés aux mots-clés gaming pour éviter le bruit web/SI)
LANG_KEYWORDS = ["c++", "c#", "csharp", "cpp"]

ALTERNANCE_KEYWORDS = [
    "alternance", "apprentissage", "apprenti", "alternant",
    "contrat pro", "contrat de professionnalisation",
]

IDF_LOCATIONS = [
    "ile-de-france", "ile de france", "idf", "paris", "montreuil",
    "boulogne", "saint-ouen", "clichy", "levallois", "neuilly",
    "suresnes", "issy", "ivry", "vincennes", "bobigny", "nanterre",
    "courbevoie", "la defense", "creteil", "versailles", "cergy",
    "evry", "massy", "palaiseau", "saclay", "malakoff", "montrouge",
    "vanves", "puteaux", "asnieres", "colombes", "noisy",
    "champs-sur-marne", "saint-denis", "aubervilliers", "pantin",
    "bagnolet", "joinville", "charenton", "gentilly", "arcueil",
    "hauts-de-seine", "seine-saint-denis", "val-de-marne",
    "yvelines", "essonne", "val-d'oise", "seine-et-marne",
]

REMOTE_KEYWORDS = ["full remote", "teletravail complet", "100% teletravail", "remote france"]


def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def find_keywords(text: str, keywords: list[str]) -> list[str]:
    norm = normalize(text)
    found = []
    for kw in keywords:
        pattern = r"(?<!\w)" + re.escape(normalize(kw)) + r"(?!\w)"
        if re.search(pattern, norm):
            found.append(kw.upper() if len(kw) <= 3 else kw.title())
    return list(dict.fromkeys(found))


def is_alternance(text: str) -> bool:
    return bool(find_keywords(text, ALTERNANCE_KEYWORDS))


def is_idf_or_remote(text: str) -> tuple[bool, str]:
    norm = normalize(text)
    # Codes postaux IDF (75xxx, 77xxx, 78xxx, 91xxx-95xxx) ou départements entre parenthèses
    if re.search(r"\b(?:75|77|78|91|92|93|94|95)\d{3}\b", norm) or re.search(
        r"\((?:75|77|78|91|92|93|94|95)\)", norm
    ):
        return True, "Île-de-France"
    if find_keywords(norm, IDF_LOCATIONS):
        return True, "Île-de-France"
    if find_keywords(norm, REMOTE_KEYWORDS):
        return True, "Remote"
    return False, ""


def is_relevant_game_prog(text: str, strict_game: bool = False) -> list[str]:
    game_hits = find_keywords(text, GAME_TECH_KEYWORDS)
    lang_hits = find_keywords(text, LANG_KEYWORDS)
    if strict_game:
        # Pour les plateformes généralistes : exige un marqueur Game Dev / Moteur 3D
        return game_hits + lang_hits if game_hits else []
    # Pour les sites déjà spécialisés jeu vidéo (AFJV) : C++/C# seul suffit
    return game_hits + lang_hits if (game_hits or lang_hits) else []


def make_id(url: str, title: str) -> str:
    raw = (url or title).strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_seen() -> set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2, ensure_ascii=False)


# --- SOURCE 1 : AFJV (Spécialiste Jeu Vidéo France) ---
def fetch_afjv() -> list[dict]:
    offers = []
    urls = [
        "https://emploi.afjv.com/index.php",
        "https://emploi.afjv.com/index.php?page=2",
    ]
    seen_links = set()

    for page_url in urls:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "emploi-jeux-video" not in href and "offre" not in href:
                    continue

                full_url = urljoin(page_url, href)
                if full_url in seen_links:
                    continue
                seen_links.add(full_url)

                # Récupère le texte du bloc parent pour avoir titre + entreprise + contrat + ville
                container = a.find_parent(["li", "article", "tr", "div"])
                block_text = container.get_text(" ", strip=True) if container else a.get_text(" ", strip=True)
                title = a.get_text(" ", strip=True)
                if len(title) < 5:
                    continue

                if not is_alternance(block_text):
                    continue

                ok_loc, loc_label = is_idf_or_remote(block_text)
                if not ok_loc:
                    continue

                tags = is_relevant_game_prog(block_text, strict_game=False)
                if not tags:
                    continue

                offers.append({
                    "id": make_id(full_url, title),
                    "title": title[:200],
                    "company": "Voir l'annonce AFJV",
                    "location": loc_label,
                    "url": full_url,
                    "source": "AFJV",
                    "tags": tags[:6],
                })
        except Exception as e:
            print(f"[AFJV] Erreur sur {page_url} : {e}")

    return offers


# --- SOURCE 2 : API LA BONNE ALTERNANCE (Gouvernement - IDF) ---
def fetch_lba() -> list[dict]:
    offers = []
    api_url = "https://labonnealternance.apprentissage.beta.gouv.fr/api/v1/jobs"
    params = {
        "romes": "M1805,E1104",  # Dev informatique & Multimédia/Jeux
        "latitude": 48.8566,
        "longitude": 2.3522,
        "radius": 60,
        "insee": "75056",
        "caller": "bot_alternance_gameprog",
    }

    try:
        resp = requests.get(api_url, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        buckets = []
        for key in ("peJobs", "matchas"):
            results = data.get(key, {}).get("results", [])
            if isinstance(results, list):
                buckets.extend(results)

        for job in buckets:
            title = job.get("title") or ""
            desc = (job.get("job", {}) or {}).get("description", "") or ""
            company = (job.get("company", {}) or {}).get("name", "Entreprise non précisée")
            city = (job.get("place", {}) or {}).get("city") or "Île-de-France"
            url = job.get("url") or ""

            full_text = f"{title} {desc} {company}"
            tags = is_relevant_game_prog(full_text, strict_game=True)
            if not tags:
                continue

            offers.append({
                "id": make_id(url, f"{title}_{company}"),
                "title": title[:200],
                "company": company[:100],
                "location": city[:100],
                "url": url or "https://labonnealternance.apprentissage.beta.gouv.fr/",
                "source": "La Bonne Alternance",
                "tags": tags[:6],
            })
    except Exception as e:
        print(f"[LBA] Erreur : {e}")

    return []


# --- SOURCE 3 : FRANCE TRAVAIL API (Optionnel, si clés fournies) ---
def fetch_france_travail() -> list[dict]:
    if not (FT_CLIENT_ID and FT_CLIENT_SECRET):
        return []

    offers = []
    try:
        token_resp = requests.post(
            "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire",
            data={
                "grant_type": "client_credentials",
                "client_id": FT_CLIENT_ID,
                "client_secret": FT_CLIENT_SECRET,
                "scope": "api_offresdemploiv2 o2dsoffre",
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        token = token_resp.json()["access_token"]
        auth_headers = {"Authorization": f"Bearer {token}"}

        queries = ["Unreal", "Unity", "C++ jeu video", "C# Unity", "Game programmer"]
        for q in queries:
            r = requests.get(
                "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search",
                headers=auth_headers,
                params={
                    "motsCles": q,
                    "region": "11",       # Île-de-France
                    "natureContrat": "E2", # Contrat d'apprentissage
                    "range": "0-24",
                },
                timeout=15,
            )
            if r.status_code not in (200, 206):
                continue

            for item in r.json().get("resultats", []):
                title = item.get("intitule", "")
                desc = item.get("description", "")
                company = (item.get("entreprise") or {}).get("nom", "Entreprise confidentielle")
                loc = (item.get("lieuTravail") or {}).get("libelle", "Île-de-France")
                url = (item.get("origineOffre") or {}).get("urlOrigine") or (
                    f"https://candidat.francetravail.fr/offres/recherche/detail/{item.get('id')}"
                )

                tags = is_relevant_game_prog(f"{title} {desc}", strict_game=True)
                if not tags:
                    continue

                offers.append({
                    "id": make_id(url, title),
                    "title": title[:200],
                    "company": company[:100],
                    "location": loc[:100],
                    "url": url,
                    "source": "France Travail",
                    "tags": tags[:6],
                })
    except Exception as e:
        print(f"[France Travail] Erreur : {e}")

    return offers


# --- ENVOI DISCORD ---
def send_to_discord(offers: list[dict]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise ValueError("DISCORD_WEBHOOK_URL n'est pas défini dans les variables d'environnement.")

    # Discord accepte jusqu'à 10 embeds par message ; on envoie par paquets de 5
    chunk_size = 5
    for i in range(0, len(offers), chunk_size):
        chunk = offers[i : i + chunk_size]
        embeds = []
        for o in chunk:
            tags_str = " • ".join(f"`{t}`" for t in o["tags"])
            embeds.append({
                "title": f"🎮 {o['title']}",
                "url": o["url"],
                "color": 0x5865F2,
                "fields": [
                    {"name": "Entreprise", "value": o["company"], "inline": True},
                    {"name": "Lieu", "value": o["location"], "inline": True},
                    {"name": "Source", "value": o["source"], "inline": True},
                    {"name": "Mots-clés détectés", "value": tags_str or "Game Dev", "inline": False},
                ],
            })

        payload = {
            "username": "Bot Alternance Game Prog",
            "content": f"🔥 **{len(offers)} nouvelle(s) offre(s) d'alternance trouvée(s) !**" if i == 0 else None,
            "embeds": embeds,
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        time.sleep(1)


def main() -> None:
    seen = load_seen()
    all_offers = []
    all_offers.extend(fetch_afjv())
    all_offers.extend(fetch_lba())
    all_offers.extend(fetch_france_travail())

    # Déduplication entre sources et par rapport à l'historique
    new_offers = []
    for offer in all_offers:
        if offer["id"] not in seen:
            seen.add(offer["id"])
            new_offers.append(offer)

    print(f"Total récupéré : {len(all_offers)} | Nouvelles offres : {len(new_offers)}")

    if new_offers:
        send_to_discord(new_offers)
        save_seen(seen)
        print("Offres envoyées sur Discord avec succès.")
    else:
        print("Aucune nouvelle offre pour le moment.")


if __name__ == "__main__":
    main()
