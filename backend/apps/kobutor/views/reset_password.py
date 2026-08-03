from django.shortcuts import render, redirect
from apps.kobutor.serializers.reset_password_serializer import ResetPasswordSerializer
from django.contrib.auth import get_user_model

User = get_user_model()

def ResetPasswordView(request):
    # NOTE: the reverse target here is ``customer:forgot-password``. This used
    # to say ``kobutor:forgot-password``, which is not a registered URL name,
    # so every unauthorised hit on this page raised NoReverseMatch (500)
    # instead of redirecting to step 1.
    user_id = request.session.get('password_reset_user_id')
    if not user_id:
        return redirect('customer:forgot-password')  # User must start from step 1

    user = User.objects.filter(id=user_id).first()
    if not user:
        return redirect('customer:forgot-password')

    if request.method == "POST":
        serializer = ResetPasswordSerializer(data=request.POST, context={'user': user})
        if serializer.is_valid():
            serializer.save()
            # Clear the session key after password reset
            del request.session['password_reset_user_id']
            return redirect("customer:signin")
        else:
            return render(request, "kobutor/reset_password.html", {
                "error": serializer.errors
            })

    return render(request, "kobutor/reset_password.html")
