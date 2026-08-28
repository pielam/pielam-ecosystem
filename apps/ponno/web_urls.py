# --------------------------------------------------------------------
# apps/ponno/web_urls.py — additions
# --------------------------------------------------------------------
#
# 1) Add these imports near the top, next to the other view imports:

from apps.ponno.views.campaign_list import CampaignListView
from apps.ponno.views.campaign_detail import CampaignDetailView
from apps.ponno.views.campaign_create import CampaignCreateView
from apps.ponno.views.campaign_edit import CampaignEditView
from apps.ponno.views.product_campaign_price import ProductCampaignPriceView

# 2) Add these path() entries to urlpatterns. Placed after the
#    subcategory block and before the wishlist block, but order doesn't
#    matter beyond campaign/create/ and campaign/<slug:slug>/edit/ needing
#    to be registered before campaign/<slug:slug>/ so "create"/"edit"
#    aren't swallowed by the slug pattern — same ordering trick already
#    used for brand/category/subcategory above:

    
   

   

# --------------------------------------------------------------------
# Full resulting urlpatterns list (for reference / copy-paste), with the
# additions above merged into your original file in place:
# --------------------------------------------------------------------


from django.urls import path
from apps.ponno.views.home import HomeEngineView
from apps.ponno.views.product_upload import ProductUploadView
from apps.ponno.views.search import AjaxSearchView
from apps.ponno.views.product_detail import ProductDetailView
from apps.ponno.views.brand_products import BrandProducts
from apps.ponno.views.category_products import CategoryProducts
from apps.ponno.views.discovery_engine_views import DiscoveryEngineView
from apps.ponno.views.product_edit import ProductEditView
from apps.ponno.views.brand_list import BrandListView
from apps.ponno.views.category_list import CategoryListView
from apps.ponno.views.sub_categories_for_category import sub_categories_for_category

from apps.ponno.views.product_actions import (
    WishlistToggleView,
    ProductRatingView,
    UserProductRatingView,
    ProductShareView,
)
from apps.ponno.views.brand_create import BrandCreateView
from apps.ponno.views.brand_edit import BrandEditView

from apps.ponno.views.category_create import CategoryCreateView
from apps.ponno.views.category_edit import CategoryEditView

from apps.ponno.views.sub_category_create import SubCategoryCreateView
from apps.ponno.views.sub_category_edit import SubCategoryEditView
from apps.ponno.views.subcategory_list import SubCategoryListView
from apps.ponno.views.subcategory_products import SubCategoryProducts

from apps.ponno.views.about import AboutView
from apps.ponno.views.privacy_policy import PrivacyPolicyView
from apps.ponno.views.terms_of_service import TermsOfServiceView

from apps.ponno.views.home import feed_load_more, feed_page_api

# --- Campaigns ---
from apps.ponno.views.campaign_list import CampaignListView
from apps.ponno.views.campaign_detail import CampaignDetailView
from apps.ponno.views.campaign_create import CampaignCreateView
from apps.ponno.views.campaign_edit import CampaignEditView
from apps.ponno.views.product_campaign_price import ProductCampaignPriceView


app_name = "ponno"

urlpatterns = [
    path('', DiscoveryEngineView, name='discovery_engine'),
    path('home', HomeEngineView, name='home'),
    path('engine/load-more/', feed_load_more, name='feed_load_more'),
    path('engine/feed/', feed_page_api, name='feed_page_api'),

    path('product/upload/', ProductUploadView, name='product_upload'),
    path('product/<slug:slug>/edit/', ProductEditView, name='product_edit'),
    path('ajax/search/', AjaxSearchView, name='ajax_search'),
    path(
        'product/<slug:slug>/campaign-price/',
        ProductCampaignPriceView,
        name='product_campaign_price',
    ),
    path('product/<slug:slug>/', ProductDetailView, name='product_detail'),

    path('brand/create/', BrandCreateView, name='brand_create'),
    path('brand/<slug:slug>/edit/', BrandEditView, name='brand_edit'),
    path('brand/', BrandListView, name='brand_list'),
    path('brand/<slug:brand_slug>/', BrandProducts, name='brand_products'),

    path('category/', CategoryListView, name='category_list'),
    path('category/create/', CategoryCreateView, name='category_create'),
    path('category/<slug:slug>/edit/', CategoryEditView, name='category_edit'),
    path('category/<slug:category_slug>/', CategoryProducts, name='category_products'),

    path("sub-categories/", sub_categories_for_category, name="sub_categories_for_category"),

    path('sub_categories/create/', SubCategoryCreateView, name='subcategory_create'),
    path('sub_categories/<slug:slug>/edit/', SubCategoryEditView, name='subcategory_edit'),
    path('subcategory/<slug:category_slug>/', SubCategoryProducts, name='subcategory_products'),
    path('subcategory/', SubCategoryListView, name='subcategory_list'),



    # Wishlist
    path('wishlist/toggle/<uuid:product_id>/', WishlistToggleView, name='wishlist_toggle'),

    # Rating
    path('rate/<uuid:product_id>/', ProductRatingView, name='rate_product'),
    path('rate/<uuid:product_id>/me/', UserProductRatingView, name='user_product_rating'),

    path('share/<uuid:product_id>/', ProductShareView, name='share_product'),

    path('about/', AboutView, name='about'),
    path('privacy-policy/', PrivacyPolicyView, name='privacy_policy'),
    path('terms-of-service/', TermsOfServiceView, name='terms_of_service'),


    # Campaigns
    path('campaign/create/', CampaignCreateView, name='campaign_create'),
    path('campaign/<slug:slug>/edit/', CampaignEditView, name='campaign_edit'),
    path('campaign/', CampaignListView, name='campaign_list'),
    path('campaign/<slug:slug>/', CampaignDetailView, name='campaign_detail'),

     # Ajax: resolved campaign price for a single product (product detail page)
    path(
            'product/<slug:slug>/campaign-price/',
            ProductCampaignPriceView,
            name='product_campaign_price',
        ),
]
