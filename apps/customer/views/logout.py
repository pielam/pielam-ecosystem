from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect, render
def LogoutView(request):
   
    # Log out the user (clear session)
    logout(request)
    
    # Redirect to sign-in page or render a template
    return redirect('customer:signin')
    