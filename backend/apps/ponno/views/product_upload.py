# apps/ponno/views/product_upload.py

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from decimal import Decimal, InvalidOperation
import logging
import random
import string

from apps.customer.models.profile_info import ProfileInfo
from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product

logger = logging.getLogger(__name__)

def generate_sku():
    """Generate unique SKU"""
    while True:
        sku = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        if not Product.objects.filter(sku=sku).exists():
            return sku

@login_required
def ProductUploadView(request):
    """
    FIXED: Handle product upload with proper Decimal conversion + SKU generation
    """
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)
    
    if user.role != 'dealer':
        messages.error(request, "❌ User must be a dealer to upload products.")
        return redirect('customer:profile')
    
    brands = Brand.objects.active_brands().order_by('brand_name')
    categories = Category.objects.active_categories().filter(parent__isnull=True).order_by('category_name')
    
    if request.method == 'GET':
        context = {
            "user": user,
            "profile_info": profile_info,
            "brands": brands,
            "categories": categories,
        }
        return render(request, "ponno/product_upload.html", context)
    
    # ==================== POST REQUEST HANDLING ====================
    if request.method == 'POST':
        try:
            with transaction.atomic():
                # ===== 1. EXTRACT RAW DATA =====
                product_title = request.POST.get('product_title', '').strip()
                product_name = request.POST.get('product_name', '').strip()
                brand_id = request.POST.get('brand')
                category_id = request.POST.get('category')
                description = request.POST.get('description', '').strip()
                brand_price_str = request.POST.get('brand_price', '').strip()
                buying_price_str = request.POST.get('buying_price', '').strip()
                selling_price_str = request.POST.get('selling_price', '').strip()
                stock_str = request.POST.get('stock', '0').strip()
                image = request.FILES.get('image')
                
                # ===== 2. VALIDATE & CONVERT TO CORRECT TYPES =====
                errors = []
                
                # Product title validation
                if not product_title:
                    errors.append("Product title is required.")
                elif len(product_title) < 3:
                    errors.append("Product title must be at least 3 characters.")
                
                # Product name validation
                if not product_name:
                    errors.append("Product name is required.")
                elif len(product_name) < 3:
                    errors.append("Product name must be at least 3 characters.")
                
                # Price conversions with validation
                try:
                    buying_price = Decimal(buying_price_str)
                    if buying_price <= 0:
                        errors.append("Buying price must be greater than 0.")
                except (InvalidOperation, ValueError):
                    errors.append("Invalid buying price format.")
                
                try:
                    selling_price = Decimal(selling_price_str) if selling_price_str else buying_price
                    if selling_price < buying_price:
                        errors.append("Selling price cannot be less than buying price.")
                except (InvalidOperation, ValueError):
                    errors.append("Invalid selling price format.")
                
                brand_price = None
                if brand_price_str:
                    try:
                        brand_price = Decimal(brand_price_str)
                    except (InvalidOperation, ValueError):
                        messages.warning(request, "⚠️ Invalid brand price format. It will be ignored.")
                
                # Stock conversion
                try:
                    stock = int(stock_str)
                    if stock < 0:
                        errors.append("Stock cannot be negative.")
                except (ValueError, TypeError):
                    errors.append("Invalid stock value.")
                
                # Show errors if any
                if errors:
                    for err in errors:
                        messages.error(request, f"❌ {err}")
                    context = {
                        "user": user,
                        "profile_info": profile_info,
                        "brands": brands,
                        "categories": categories,
                        "form_data": request.POST,
                    }
                    return render(request, "ponno/product_upload.html", context)
                
                # ===== 3. GENERATE UNIQUE SKU =====
                sku = generate_sku()
                
                # ===== 4. CREATE PRODUCT WITH CORRECT TYPES =====
                product = Product(
                    product_title=product_title,
                    product_name=product_name,
                    dealer=user,
                    description=description,
                    buying_price=buying_price,
                    selling_price=selling_price,
                    brand_price=brand_price,
                    stock=stock,
                    image=image,
                    sku=sku,  # ✅ ADD THIS LINE
                    currency='BDT',
                    is_active=True,
                    is_verified=False,
                )
                
                # Assign brand if valid
                if brand_id:
                    try:
                        brand = Brand.objects.get(id=brand_id, is_active=True, deleted_at__isnull=True)
                        product.brand = brand
                    except Brand.DoesNotExist:
                        messages.warning(request, "⚠️ Selected brand not found. Product uploaded without brand.")
                
                # Assign category if valid
                if category_id:
                    try:
                        category = Category.objects.get(id=category_id, is_active=True, deleted_at__isnull=True)
                        product.category = category
                    except Category.DoesNotExist:
                        messages.warning(request, "⚠️ Selected category not found. Product uploaded without category.")
                
                # ===== 5. SAVE (triggers slug gen, price calc, etc.) =====
                product.save()
                
                # ===== 6. SUCCESS =====
                messages.success(
                    request,
                    f"✅ Product '{product.product_name}' uploaded successfully! "
                    f"SKU: {product.sku} | Final Price: {product.final_price} BDT"
                )
                return redirect('ponno:home')
                
        except Exception as e:
            logger.exception("Product upload failed")
            messages.error(request, f"❌ Upload failed: {str(e)}")
            context = {
                "user": user,
                "profile_info": profile_info,
                "brands": brands,
                "categories": categories,
                "form_data": request.POST,
            }
            return render(request, "ponno/product_upload.html", context)
    
    return redirect('ponno:home')