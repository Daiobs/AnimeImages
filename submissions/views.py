# Create your views here.
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from .forms import SubmissionForm
from .models import Submission

def submit_post(request):
    if request.method == "POST":
        form = SubmissionForm(request.POST, request.FILES)  # 添加 request.FILES 以支持文件上传
        if form.is_valid():
            form.save()
            # 投稿成功后重定向到列表页面
            return redirect(reverse('submission_list'))
    else:
        form = SubmissionForm()
    return render(request, 'submissions/submit_post.html', {'form': form})
# 投稿列表视图
def submission_list(request):
    submissions = Submission.objects.all().order_by('-created_at')
    paginator = Paginator(submissions, 40)  # 每页展示40条（10行×4列）

    page_number = request.GET.get('page')  # 获取当前页码
    page_obj = paginator.get_page(page_number)

    return render(request, 'submissions/submission_list.html', {'page_obj': page_obj})
# 投稿详情视图
def submission_detail(request, submission_id):
    submission = get_object_or_404(Submission, id=submission_id)
    return render(request, 'submissions/submission_detail.html', {'submission': submission})