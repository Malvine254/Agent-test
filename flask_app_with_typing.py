import os
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# === Import Services ===
from services.intent_service import classify_intent
from services.weather_service import get_weather
from services.time_service import get_current_time
from services.document_service import (
    allowed_file, load_all_docs_text, find_relevant_docs, generate_references_html
)
from services.azure_service import client, DEPLOYMENT_NAME

# === Load Env ===
load_dotenv()

app = Flask(__name__)
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# =====================================================
# ✅ REVERSE GEOCODING ENDPOINT (GET REAL CITY FROM LAT/LON)
# =====================================================
@app.route("/get_city", methods=["POST"])
def get_city():
    """Reverse-geocode coordinates to find the nearest real city."""
    data = request.get_json()
    lat = data.get("lat")
    lon = data.get("lon")
    api_key = os.getenv("OPENWEATHER_API_KEY")

    if not lat or not lon:
        return jsonify({"city": "Unknown"}), 400

    try:
        # --- Step 1: OpenWeather reverse geocode (fast + global)
        if api_key:
            ow_url = f"https://api.openweathermap.org/geo/1.0/reverse?lat={lat}&lon={lon}&limit=1&appid={api_key}"
            ow_res = requests.get(ow_url, timeout=8)
            ow_data = ow_res.json()
            if isinstance(ow_data, list) and ow_data:
                city = (
                    ow_data[0].get("name")
                    or ow_data[0].get("local_names", {}).get("en")
                    or None
                )
                if city and city.lower() not in ["unknown"]:
                    print(f"🌍 Detected city via OpenWeather: {city}")
                    return jsonify({"city": city})

        # --- Step 2: Fallback — BigDataCloud (better accuracy)
        bdc_url = f"https://api.bigdatacloud.net/data/reverse-geocode-client?latitude={lat}&longitude={lon}&localityLanguage=en"
        bdc_res = requests.get(bdc_url, timeout=8)
        bdc = bdc_res.json()
        city = (
            bdc.get("locality")
            or bdc.get("city")
            or bdc.get("principalSubdivision")
            or "Unknown"
        )
        print(f"🗺️ Fallback detected city: {city}")
        return jsonify({"city": city})

    except Exception as e:
        print("⚠️ Reverse geocode failed:", e)
        return jsonify({"city": "Unknown"})


# =====================================================
# MAIN ROUTES
# =====================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files["file"]
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(path)
        return jsonify({"message": f"{filename} uploaded successfully"})
    return jsonify({"error": "Invalid file format"}), 400


# =====================================================
# 🧠 CHAT ROUTE WITH TYPING INDICATOR (Teams-style approach)
# =====================================================
@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "").strip()
    user_lat = request.cookies.get("lat")
    user_lon = request.cookies.get("lon")
    user_city = request.cookies.get("city")

    # Send typing indicator immediately (similar to Teams bot pattern)
    # In Teams: await ctx.send({"type": "typing"})
    # In Flask REST: Return typing status in JSON for frontend to display
    
    try:
        intent = classify_intent(user_message)
        print(f"🧠 Intent: {intent}")
        print(
            f"📍 User location: {user_city or 'Unknown city'} "
            f"({float(user_lat):.2f}, {float(user_lon):.2f})"
            if user_lat and user_lon
            else "📍 No coordinates"
        )

        # --- LOCATION REQUEST ---
        if any(x in user_message.lower() for x in ["my location", "where am i", "current location", "show location"]):
            if user_city and user_lat and user_lon:
                weather_info = get_weather(lat=user_lat, lon=user_lon)
                location_html = f"""
                <div class='card p-3 bg-light border'>
                    <h5 class='fw-bold mb-2'>📍 Your Current Location</h5>
                    <p>You're currently near <b>{user_city}</b> 
                    (<code>{float(user_lat):.2f}, {float(user_lon):.2f}</code>).</p>
                    <hr>
                    {weather_info}
                    <p class='text-muted small mt-2'>
                        You can update your location anytime using the "Change My Location" button above.
                    </p>
                </div>
                """
                return jsonify({"response": location_html, "type": "message"})
            return jsonify({
                "response": "<p>⚠️ I can't determine your location yet. Please enable location access in the chat above.</p>",
                "type": "message"
            })

        # --- WEATHER HANDLING ---
        if intent == "weather":
            specific_city = None
            for word in user_message.split():
                if word[0].isupper() and len(word) > 3:
                    specific_city = word
                    break

            if "here" in user_message.lower() or "my location" in user_message.lower():
                if user_lat and user_lon:
                    return jsonify({"response": get_weather(lat=user_lat, lon=user_lon), "type": "message"})
                elif user_city:
                    return jsonify({"response": get_weather(city=user_city), "type": "message"})
                else:
                    return jsonify({"response": "<p>❌ Location not found. Please enable location access.</p>", "type": "message"})
            elif specific_city:
                return jsonify({"response": get_weather(city=specific_city), "type": "message"})
            else:
                return jsonify({"response": get_weather(city=user_city), "type": "message"})

        # --- TIME HANDLING ---
        if intent == "time":
            return jsonify({"response": get_current_time(), "type": "message"})

        # --- DOCUMENT HANDLING ---
        if intent == "document" or any(
            x in user_message.lower()
            for x in ["file", "document", "report", "pdf", "upload", "summarize", "extract"]
        ):
            docs_dict = load_all_docs_text(UPLOAD_FOLDER)
            if not docs_dict:
                return jsonify({"response": "<p>No documents uploaded yet.</p>", "type": "message"})

            # Find relevant docs using embeddings
            relevant_docs, context = find_relevant_docs(user_message, docs_dict)

            if not context.strip():
                return jsonify({"response": "<p>❌ I couldn't find relevant info in your uploaded documents.</p>", "type": "message"})

            prompt = f"""
You are an AI assistant that answers questions using only the provided document context.
Never use external knowledge or assumptions.
If the answer is not found, say:
"I could not find that information in the uploaded documents."

<context>
{context}
</context>

Question: {user_message}
"""

            res = client.chat.completions.create(
                model=DEPLOYMENT_NAME,
                messages=[
                    {"role": "system", "content": "Always answer strictly from <context> using clean HTML (<p>, <b>, <ul>, <li>), never Markdown."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=900,
            )

            html = res.choices[0].message.content.strip()
            refs_html = generate_references_html(relevant_docs)
            return jsonify({"response": html + refs_html, "type": "message"})

        # --- GENERAL KNOWLEDGE HANDLING ---
        if intent == "chat" or intent == "general":
            res = client.chat.completions.create(
                model=DEPLOYMENT_NAME,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Answer general knowledge queries in clean, valid HTML. "
                            "Use headings, lists, and bold tags — no Markdown syntax."
                        ),
                    },
                    {"role": "user", "content": user_message},
                ],
                temperature=0.6,
                max_tokens=400,
            )
            html = res.choices[0].message.content.strip()
            return jsonify({"response": html, "type": "message"})

        # --- FALLBACK ---
        return jsonify({
            "response": "<p>🤖 Sorry, I couldn't understand that. Please ask a clear question or upload a document for analysis.</p>",
            "type": "message"
        })
        
    except Exception as e:
        print(f"⚠️ Chat error: {e}")
        return jsonify({
            "response": "<p>❌ An error occurred. Please try again.</p>",
            "type": "error"
        }), 500


# =====================================================
# FILE MANAGEMENT ROUTES
# =====================================================
@app.route("/uploads/<path:filename>")
def serve_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/files", methods=["GET"])
def list_files():
    files = [f for f in os.listdir(UPLOAD_FOLDER) if allowed_file(f)]
    return jsonify(files)


@app.route("/delete/<filename>", methods=["DELETE"])
def delete_file(filename):
    path = os.path.join(UPLOAD_FOLDER, filename)
    if os.exists(path):
        os.remove(path)
        return jsonify({"message": f"{filename} deleted successfully"})
    return jsonify({"error": "File not found"}), 404


# =====================================================
# ENTRY POINT
# =====================================================
if __name__ == "__main__":
    app.run(debug=True)
