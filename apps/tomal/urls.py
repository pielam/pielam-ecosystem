from django.urls import path
from apps.customer.views.signup import SignUpView
from apps.tomal.views import DashboardView, PatientsView, AppointmentView,TestandPackagesView, SamplecollectionView, ReportsView, InventoryView, BillingView, DoctorsView, StuffView, AnalyticsView, SettingsView, SearchView, NotificationView, ProfileView, HomeView

app_name = "tomal"

urlpatterns = [
    path('dashboard/', DashboardView, name='dashboard'),
    path('patients/', PatientsView, name='patients'),
    path('appointments/', AppointmentView, name='appointments'),
    path('testandpackages/', TestandPackagesView, name='testandpackages'),
    path('samplecollection/', SamplecollectionView, name='samplecollection'),
    path('reports/', ReportsView, name='reports'),
    path('inventory/', InventoryView, name='inventory'),
    path('billing/', BillingView, name='billing'),
    path('doctors/', DoctorsView, name='doctors'),
    path('stuff/', StuffView, name='stuff'),
    path('analytics/', AnalyticsView, name='analytics'),
    path('settings/', SettingsView, name='settings'),
    path('search/', SearchView, name='search'),
    path('notification/', NotificationView, name='notification'),
    path('profile/', ProfileView, name='profile'),
    path('home/', HomeView, name='home'),
       
]
