import os
import sqlite3
from flask import Flask, request, render_template, jsonify, g
from geopy.distance import geodesic
from geopy.geocoders import Nominatim

app = Flask(__name__)

###############################################################################
# GESTION DU COMPTEUR DE VISITEURS (PERSISTANT via SQLite)
###############################################################################
DATABASE = 'visitors.db'

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
    return db

def init_db():
    """Initialise la base de données et crée la table pour le compteur si nécessaire."""
    with app.app_context():
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS visitor (
                id INTEGER PRIMARY KEY,
                count INTEGER NOT NULL
            )
        """)
        cur = db.execute("SELECT count(*) FROM visitor")
        if cur.fetchone()[0] == 0:
            db.execute("INSERT INTO visitor (id, count) VALUES (1, 0)")
        db.commit()

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

@app.route("/increment_visitor_count", methods=["POST"])
def increment_visitor_count():
    """
    Incrémente le compteur persistant dans la BDD et renvoie la nouvelle valeur en JSON.
    """
    db = get_db()
    db.execute("UPDATE visitor SET count = count + 1 WHERE id = 1")
    db.commit()
    cur = db.execute("SELECT count FROM visitor WHERE id = 1")
    new_count = cur.fetchone()[0]
    return jsonify({"visitor_count": new_count})

@app.before_first_request
def initialize_database():
    init_db()

###############################################################################
# DONNÉES EXISTANTES
###############################################################################
TARGET_CITIES_FR = [
    ("Paris",       48.8566,  2.3522, 35.0),
    ("Marseille",   43.2965,  5.3698, 17.0),
    ("Lyon",        45.7640,  4.8357, 170.0),
    ("Toulouse",    43.6047,  1.4442, 146.0),
    ("Bordeaux",    44.8378, -0.5792, 16.0),
    ("Strasbourg",  48.5839,  7.7455, 140.0),
    ("Nice",        43.7102,  7.2620, 15.0),
    ("Nantes",      47.2184, -1.5536, 20.0),
    ("Lille",       50.6292,  3.0573, 20.0),
    ("Rennes",      48.1173, -1.6778, 45.0),
    ("Montpellier", 43.6108,  3.8767, 48.0)
]

BOMB_TYPES = {
    "Fission (classique)": {
        "fireball": 1.2,
        "blast":    4.5,
        "thermal":  6.0,
        "fallout":  20.0
    },
    "Fusion (Hydrogène)": {
        "fireball": 1.5,
        "blast":    5.0,
        "thermal":  8.0,
        "fallout":  30.0
    },
    "Neutron": {
        "fireball": 1.0,
        "blast":    3.0,
        "thermal":  7.0,
        "fallout":  25.0
    },
    "Thermobaric (non-nuc)": {
        "fireball": 0.4,
        "blast":    1.5,
        "thermal":  0.0,
        "fallout":  0.0
    }
}

TERRAIN_FACTORS = {
    "plaine":       {"amplify_blast": 1.0,  "reduce_thermal": 1.0,  "trap_fallout": 1.0},
    "colline":      {"amplify_blast": 0.9,  "reduce_thermal": 0.95, "trap_fallout": 1.1},
    "montagne":     {"amplify_blast": 0.7,  "reduce_thermal": 0.8,  "trap_fallout": 1.2},
    "vallee_fermee":{"amplify_blast": 0.6,  "reduce_thermal": 0.8,  "trap_fallout": 1.3},
    "canyon":       {"amplify_blast": 0.5,  "reduce_thermal": 0.7,  "trap_fallout": 1.4}
}

# Initialise un géocodeur
geolocator = Nominatim(user_agent="bomba_latina_app")

###############################################################################
# FONCTIONS DE CALCULS
###############################################################################
def compute_zones(kilotons, bomb_type):
    if bomb_type not in BOMB_TYPES:
        bomb_type = "Fission (classique)"  # fallback
    kt_cubic = kilotons ** (1.0/3.0)
    c = BOMB_TYPES[bomb_type]
    zones = {
        "fireball": c["fireball"] * kt_cubic,
        "blast":    c["blast"]    * kt_cubic,
        "thermal":  c["thermal"]  * kt_cubic,
        "fallout":  c["fallout"]  * kt_cubic
    }
    return zones

def adjust_zones_for_terrain(zones, terrain_type):
    if terrain_type not in TERRAIN_FACTORS:
        return zones
    t = TERRAIN_FACTORS[terrain_type]
    zones["blast"]   *= t["amplify_blast"]
    zones["thermal"] *= t["reduce_thermal"]
    zones["fallout"] *= t["trap_fallout"]
    return zones

def adjust_zones_for_altitude(zones, alt_target, alt_user):
    delta_alt = alt_user - alt_target
    if delta_alt < -50:
        factor = 1.0 + 0.02 * (abs(delta_alt) / 10.0)
        zones["fallout"] *= factor
    elif delta_alt > 50:
        factor = 1.0 - 0.01 * (delta_alt / 10.0)
        if factor < 0.5:
            factor = 0.5
        zones["blast"] *= factor
    return zones

def compute_lethality_score(distance, zones):
    if distance <= zones["fireball"]:
        return 100
    elif distance <= zones["blast"]:
        return 80
    elif distance <= zones["thermal"]:
        return 50
    elif distance <= zones["fallout"]:
        return 15
    else:
        return 0

###############################################################################
# ROUTE PRINCIPALE
###############################################################################
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        # 1) Type de bombe
        bomb_type = request.form.get("bomb_type", "Fission (classique)")
        
        # 2) Puissance
        try:
            bomb_megatons = float(request.form.get("bomb_megatons", 1.0))
        except:
            bomb_megatons = 1.0
        bomb_kilotons = bomb_megatons * 1000.0
        
        # 3) Ville-cible
        selected_target_index = request.form.get("selected_target")
        if selected_target_index:
            idx = int(selected_target_index)
            lat_target = TARGET_CITIES_FR[idx][1]
            lon_target = TARGET_CITIES_FR[idx][2]
            alt_target = TARGET_CITIES_FR[idx][3]
        else:
            lat_target, lon_target, alt_target = (48.8566, 2.3522, 35.0)
        
        # 4) Topographie
        terrain_type = request.form.get("terrain_type", "plaine")
        try:
            alt_target = float(request.form.get("alt_target", 35.0))
        except:
            alt_target = 35.0

        # 5) Localisation user
        user_city_name = request.form.get("user_city_name", "").strip()
        user_coords_text = request.form.get("user_coords", "").strip()
        user_lat = 0.0
        user_lon = 0.0
        alt_user = 0.0
        try:
            alt_user = float(request.form.get("alt_user", 0.0))
        except:
            alt_user = 0.0
        
        if user_city_name:
            try:
                location = geolocator.geocode(f"{user_city_name}, France")
                if location:
                    user_lat = location.latitude
                    user_lon = location.longitude
            except:
                pass
        
        if (user_lat == 0.0 and user_lon == 0.0) and user_coords_text:
            try:
                lat, lon = map(float, user_coords_text.split(","))
                user_lat, user_lon = lat, lon
            except:
                pass
        
        # 6) Distance
        user_coords = (user_lat, user_lon)
        target_coords = (lat_target, lon_target)
        distance_km = geodesic(user_coords, target_coords).km

        # 7) Zones
        zones = compute_zones(bomb_kilotons, bomb_type)
        zones = adjust_zones_for_terrain(zones, terrain_type)
        zones = adjust_zones_for_altitude(zones, alt_target, alt_user)

        # 8) Lethality
        lethal_score = compute_lethality_score(distance_km, zones)
        zones_rounded = {k: round(v, 2) for k, v in zones.items()}
        
        return render_template(
            "result.html",
            bomb_type=bomb_type,
            bomb_megatons=bomb_megatons,
            bomb_kilotons=bomb_kilotons,
            bomb_coords=(lat_target, lon_target),
            alt_target=alt_target,
            user_coords=(user_lat, user_lon),
            alt_user=alt_user,
            distance_km=round(distance_km, 2),
            terrain_type=terrain_type,
            zones=zones_rounded,
            lethal_score=lethal_score
        )
    
    return render_template(
        "home.html",
        bomb_types=BOMB_TYPES,
        target_cities=TARGET_CITIES_FR,
        terrain_factors=TERRAIN_FACTORS
    )

###############################################################################
# LANCEMENT DE L'APP
###############################################################################
if __name__ == "__main__":
    init_db() 
    app.run(debug=True, port=1337)
