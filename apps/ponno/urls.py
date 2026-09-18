from django.urls import path
from apps.ponno.views.home import HomeView
from apps.ponno.views.product_upload import ProductUploadView
from apps.ponno.views.search import AjaxSearchView
from apps.ponno.views.product_detail import ProductDetail
from apps.ponno.views.brand_products import BrandProducts
from apps.ponno.views.category_products import CategoryProducts
# Import other views similarly if needed


app_name = "ponno"  # Namespace for URL names

urlpatterns = [
    path('', HomeView, name='home'),
    path('product/upload/', ProductUploadView, name='product_upload'),
    path('ajax/search/', AjaxSearchView, name='ajax_search'),
    
    path('product/<slug:slug>/', ProductDetail, name='product_detail'),
    path('brand/<slug:brand_slug>/', BrandProducts, name='brand_products'),
    path('category/<slug:category_slug>/', CategoryProducts, name='category_products'),
  
]
