from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import List, Optional
from supabase import create_client, Client
import os
import jwt
from datetime import datetime
from order_tracking import router as order_tracking_router
from chat import router as chat_router

# Environment variables for Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL', 'YOUR_SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'YOUR_SUPABASE_SERVICE_ROLE_KEY')
JWT_SECRET = os.getenv('JWT_SECRET', 'YOUR_JWT_SECRET')

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()
app.include_router(order_tracking_router)
app.include_router(chat_router)
security = HTTPBearer()

ORDER_STATUSES = ['Placed', 'Accepted', 'Out for Delivery', 'Delivered']

# --- Models ---
class OrderStatusUpdate(BaseModel):
    new_status: str

class OrderItem(BaseModel):
    id: int
    order_id: int
    item_name: str
    quantity: int
    price: float

class Order(BaseModel):
    id: int
    buyer_id: str
    seller_id: str
    status: str
    status_timestamps: dict
    created_at: str
    items: List[OrderItem]

# --- Auth helpers ---
def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")

# --- API Endpoints ---
@app.get("/orders/{user_id}", response_model=List[Order])
def get_orders(user_id: str, request: Request, user=Depends(get_current_user)):
    # Only allow access if user is the same as user_id or is a seller for those orders
    role = user.get('role')
    if user['sub'] != user_id and role != 'seller':
        raise HTTPException(status_code=403, detail="Forbidden")
    # Fetch orders where user is buyer or seller
    if role == 'buyer':
        query = supabase.table('orders').select('*').eq('buyer_id', user_id)
    elif role == 'seller':
        query = supabase.table('orders').select('*').eq('seller_id', user_id)
    else:
        raise HTTPException(status_code=403, detail="Invalid role")
    orders_resp = query.execute()
    if not orders_resp.data:
        return []
    orders = orders_resp.data
    # Fetch order items for each order
    order_ids = [o['id'] for o in orders]
    items_resp = supabase.table('order_items').select('*').in_('order_id', order_ids).execute()
    items_by_order = {}
    for item in items_resp.data:
        items_by_order.setdefault(item['order_id'], []).append(item)
    # Attach items to orders
    for o in orders:
        o['items'] = items_by_order.get(o['id'], [])
    return orders

@app.post("/orders/{order_id}/status")
def update_order_status(order_id: int, status_update: OrderStatusUpdate, user=Depends(get_current_user)):
    # Only sellers can update status
    if user.get('role') != 'seller':
        raise HTTPException(status_code=403, detail="Only sellers can update order status")
    # Fetch order
    order_resp = supabase.table('orders').select('*').eq('id', order_id).single().execute()
    order = order_resp.data
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order['seller_id'] != user['sub']:
        raise HTTPException(status_code=403, detail="You can only update your own orders")
    # Validate status transition
    current_status = order['status']
    new_status = status_update.new_status
    if new_status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    if ORDER_STATUSES.index(new_status) != ORDER_STATUSES.index(current_status) + 1:
        raise HTTPException(status_code=400, detail="Invalid status transition")
    # Update status and timestamp
    status_timestamps = order.get('status_timestamps') or {}
    status_timestamps[new_status] = datetime.utcnow().isoformat()
    update_resp = supabase.table('orders').update({
        'status': new_status,
        'status_timestamps': status_timestamps
    }).eq('id', order_id).execute()
    if update_resp.error:
        raise HTTPException(status_code=500, detail="Failed to update order status")
    # (Optional) Push update to Supabase Realtime here
    return {"message": "Order status updated", "order_id": order_id, "new_status": new_status}