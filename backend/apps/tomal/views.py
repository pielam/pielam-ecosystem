from django.shortcuts import render

# Create your views here.
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError

from apps.customer.validators import email_or_phone_validator
from django.contrib.auth import get_user_model

User = get_user_model()
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from apps.customer.models.profile_info import ProfileInfo
from apps.customer.models.account import User

from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product


def DashboardView(request):
   
    context = {
       
    }
    return render(request, "tomal/dashboard.html", context)

def PatientsView(request):
   
    context = {
       
    }
    return render(request, "tomal/patients.html", context)

def AppointmentView(request):
   
    context = {
       
    }
    return render(request, "tomal/appointments.html", context)

def TestandPackagesView(request):
   
    context = {
       
    }
    return render(request, "tomal/testandpackages.html", context)

def SamplecollectionView(request):
   
    context = {
       
    }
    return render(request, "tomal/samplecollection.html", context)

def ReportsView(request):
   
    context = {
       
    }
    return render(request, "tomal/reports.html", context)

def InventoryView(request):
   
    context = {
       
    }
    return render(request, "tomal/inventory.html", context)


def BillingView(request):
   
    context = {
       
    }
    return render(request, "tomal/billing.html", context)

def DoctorsView(request):
   
    context = {
       
    }
    return render(request, "tomal/doctors.html", context)

def StuffView(request):
   
    context = {
       
    }
    return render(request, "tomal/stuff.html", context)

def AnalyticsView(request):
   
    context = {
       
    }
    return render(request, "tomal/analytics.html", context)

def SettingsView(request):
   
    context = {
       
    }
    return render(request, "tomal/settings.html", context)

def SearchView(request):
   
    context = {
       
    }
    return render(request, "tomal/search.html", context)

def NotificationView(request):
   
    context = {
       
    }
    return render(request, "tomal/notification.html", context)

def ProfileView(request):
   
    context = {
       
    }
    return render(request, "tomal/profile.html", context)

def HomeView(request):
   
    context = {
       
    }
    return render(request, "tomal/home.html", context)


