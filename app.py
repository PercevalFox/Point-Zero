from flask import Flask, request, render_template
from geopy.distance import geodesic
from geopy.geocoders import Nominatim

app = Flask(__name__)

# Liste de villes françaises “cibles probables”
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

# Types de bombes (coefficients simplifiés)
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

# Quelques paramètres de terrain
TERRAIN_FACTORS = {
    # “amplify_blast” = facteur multiplicatif sur le rayon de blast
    # “reduce_thermal” = facteur multiplicatif sur le rayon thermal
    # “trap_fallout” = facteur sur fallout (par ex, une vallée peut piéger plus de retombées)
    "plaine":       {"amplify_blast": 1.0,  "reduce_thermal": 1.0,  "trap_fallout": 1.0},
    "colline":      {"amplify_blast": 0.9,  "reduce_thermal": 0.95, "trap_fallout": 1.1},
    "montagne":     {"amplify_blast": 0.7,  "reduce_thermal": 0.8,  "trap_fallout": 1.2},
    "vallee_fermee":{"amplify_blast": 0.6,  "reduce_thermal": 0.8,  "trap_fallout": 1.3},
    "canyon":       {"amplify_blast": 0.5,  "reduce_thermal": 0.7,  "trap_fallout": 1.4}
}

geolocator = Nominatim(user_agent="bomba_latina_app")


def compute_zones(kilotons, bomb_type):
    """
    Calcule les rayons de base (fireball, blast, thermal, fallout) en km, 
    selon le type de bombe. (KT)^(1/3) * coefficient
    """
    if bomb_type not in BOMB_TYPES:
        bomb_type = "Fission (classique)"

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
    """
    Ajuste les zones en fonction du terrain.
    Modification du rayon de blast, thermal, fallout en fonction des coefficients.
    """
    if terrain_type not in TERRAIN_FACTORS:
        return zones

    t = TERRAIN_FACTORS[terrain_type]
    
    zones["blast"]   *= t["amplify_blast"]
    zones["thermal"] *= t["reduce_thermal"]
    zones["fallout"] *= t["trap_fallout"]
    
    return zones


def adjust_zones_for_altitude(zones, alt_target, alt_user):
    """
    Ajuste les rayons en fonction de la différence d'altitude (simplifié).
    - Si le user est nettement plus bas que la cible (et bomb surface), 
      on peut imaginer un renforcement de fallout qui suit la vallée.
    - Si le user est plus haut, l’onde de choc est peut-être un peu moins forte.
    
    On fait des hypothèses archi-simplifiées :
       delta_alt = alt_user - alt_target
       if delta_alt < 0 => user + bas => fallout * 1.1^(abs(delta_alt)/1000)
       if delta_alt > 0 => user + haut => blast * 0.95^(delta_alt/1000)
    """
    # Différence d’altitude
    delta_alt = alt_user - alt_target

    # Si l’utilisateur est nettement plus bas
    if delta_alt < -50:  # 50m en dessous
        factor = 1.0
        # On amplifie un peu la zone de fallout 
        # => plus on descend, plus le fallout “stagne” (hypothèse)
        # Ex: 0.02 par 10m, arbitraire
        factor += 0.02 * (abs(delta_alt) / 10.0)
        zones["fallout"] *= factor

    # S’il est plus haut
    elif delta_alt > 50:
        # On diminue un peu l'effet de blast 
        # ex: 0.01 par 10m
        factor = 1.0
        factor -= 0.01 * (delta_alt / 10.0)
        if factor < 0.5:
            factor = 0.5 
        zones["blast"] *= factor

    return zones


def compute_lethality_score(distance, zones):
    """
    Détermine un “score de létalité” (0%-100%) en fonction 
    de la zone la plus intense dans laquelle se situe l’utilisateur.
    """
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


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        
        # 1) Type de bombe
        bomb_type = request.form.get("bomb_type", "Fission (classique)")
        
        # 2) Puissance en mégatonnes
        try:
            bomb_megatons = float(request.form.get("bomb_megatons", 1.0))
        except:
            bomb_megatons = 1.0
        bomb_kilotons = bomb_megatons * 1000.0
        
        # 3) Ville-cible + altitude
        selected_target_index = request.form.get("selected_target")
        if selected_target_index:
            idx = int(selected_target_index)
            lat_target = TARGET_CITIES_FR[idx][1]
            lon_target = TARGET_CITIES_FR[idx][2]
            alt_target = TARGET_CITIES_FR[idx][3]
        else:
            # fallback sur Paris
            lat_target, lon_target, alt_target = (48.8566, 2.3522, 35.0)
        
        # 4) Topologie autour de la cible
        terrain_type = request.form.get("terrain_type", "plaine")  
        # altitude approx de la cible
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
        
        # Sinon coords manuelles
        if (user_lat == 0.0 and user_lon == 0.0) and user_coords_text:
            try:
                lat, lon = map(float, user_coords_text.split(","))
                user_lat, user_lon = lat, lon
            except:
                pass
        
        # 6) Calcul de la distance
        user_coords = (user_lat, user_lon)
        target_coords = (lat_target, lon_target)
        distance_km = geodesic(user_coords, target_coords).km

        # 7) Calcul des zones “de base”
        zones = compute_zones(bomb_kilotons, bomb_type)

        # 8) Ajustement en fonction de la topographie (cible)
        zones = adjust_zones_for_terrain(zones, terrain_type)

        # 9) Ajustement en fonction de la différence d’altitude
        zones = adjust_zones_for_altitude(zones, alt_target, alt_user)

        # 10) Score de létalité
        lethal_score = compute_lethality_score(distance_km, zones)
        
        # Arrondis pour affichage
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
    
    return render_template("home.html",
                           bomb_types=BOMB_TYPES,
                           target_cities=TARGET_CITIES_FR,
                           terrain_factors=TERRAIN_FACTORS)


if __name__ == "__main__":
    app.run(debug=True, port=1337)
