# apps/ponno/forms.py
from django import forms
from apps.ponno.models.product import Product

class ProductUploadForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            'product_name', 'brand', 'category', 'description', 'image',
            'brand_price', 'buying_price', 'selling_price', 'stock',
        ]
