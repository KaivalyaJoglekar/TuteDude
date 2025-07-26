from fastapi import APIRouter, HTTPException, Depends, Request, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import List, Optional
from supabase import create_client, Client
import os
import jwt
import requests
from datetime import datetime

# Environment variables for Supabase and Google Translate
SUPABASE_URL = os.getenv('SUPABASE_URL', 'YOUR_SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'YOUR_SUPABASE_SERVICE_ROLE_KEY')
JWT_SECRET = os.getenv('JWT_SECRET', 'YOUR_JWT_SECRET')
GOOGLE_TRANSLATE_API_KEY = os.getenv('GOOGLE_TRANSLATE_API_KEY', 'YOUR_GOOGLE_TRANSLATE_API_KEY')

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

router = APIRouter()
security = HTTPBearer()

# --- Models ---
class SendMessageRequest(BaseModel):
    receiver_id: str
    message: str

class Message(BaseModel):
    id: int
    sender_id: str
    receiver_id: str
    message: str
    translated_message: Optional[str]
    timestamp: str

# --- Auth helpers ---
def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")

# --- Translation helper ---
def translate_text(text, target_lang, source_lang=None):
    url = f"https://translation.googleapis.com/language/translate/v2"
    params = {
        'q': text,
        'target': target_lang,
        'key': GOOGLE_TRANSLATE_API_KEY
    }
    if source_lang:
        params['source'] = source_lang
    resp = requests.post(url, data=params)
    if resp.status_code == 200:
        data = resp.json()
        return data['data']['translations'][0]['translatedText']
    else:
        return None

# --- API Endpoints ---
@router.post("/messages/send")
def send_message(req: SendMessageRequest, user=Depends(get_current_user)):
    sender_id = user['sub']
    receiver_id = req.receiver_id
    message = req.message
    # Get sender and receiver preferred languages
    sender_profile = supabase.table('profiles').select('preferred_language').eq('id', sender_id).single().execute().data
    receiver_profile = supabase.table('profiles').select('preferred_language').eq('id', receiver_id).single().execute().data
    sender_lang = sender_profile.get('preferred_language', 'en') if sender_profile else 'en'
    receiver_lang = receiver_profile.get('preferred_language', 'en') if receiver_profile else 'en'
    # Translate if needed
    translated_message = None
    if sender_lang != receiver_lang:
        translated_message = translate_text(message, receiver_lang, sender_lang)
    # Insert message into Supabase
    insert_resp = supabase.table('messages').insert({
        'sender_id': sender_id,
        'receiver_id': receiver_id,
        'message': message,
        'translated_message': translated_message
    }).execute()
    if insert_resp.error:
        raise HTTPException(status_code=500, detail="Failed to send message")
    return {"message": "Message sent"}

@router.get("/messages/{conversation_id}", response_model=List[Message])
def get_messages(conversation_id: str, user=Depends(get_current_user),
                 limit: int = Query(50, ge=1, le=100),
                 offset: int = Query(0, ge=0)):
    # conversation_id is a string like "user1_user2" (sorted by id)
    user_ids = conversation_id.split('_')
    if len(user_ids) != 2:
        raise HTTPException(status_code=400, detail="Invalid conversation_id")
    if user['sub'] not in user_ids:
        raise HTTPException(status_code=403, detail="Forbidden")
    # Fetch messages between the two users
    query = supabase.table('messages').select('*') \
        .or_(f"and(sender_id.eq.{user_ids[0]},receiver_id.eq.{user_ids[1]}),and(sender_id.eq.{user_ids[1]},receiver_id.eq.{user_ids[0]})") \
        .order('timestamp', desc=False) \
        .range(offset, offset + limit - 1)
    resp = query.execute()
    if resp.error:
        raise HTTPException(status_code=500, detail="Failed to fetch messages")
    return resp.data

# --- Example SQL for Supabase schema ---
# create table messages (
#   id bigserial primary key,
#   sender_id uuid references profiles(id),
#   receiver_id uuid references profiles(id),
#   message text not null,
#   translated_message text,
#   timestamp timestamptz default now()
# );
#
# create trigger set_message_timestamp before insert on messages
# for each row execute procedure set_current_timestamp();
#
# -- profiles table should have a preferred_language column
# alter table profiles add column preferred_language text default 'en'; 