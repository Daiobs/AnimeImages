from django import forms
from .models import Submission


class SubmissionForm(forms.ModelForm):
    class Meta:
        model = Submission
        fields = ['title', 'category', 'image', 'video_link', 'content']
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '请输入标题',
                'required': True,  # 标题必填
            }),
            'category': forms.Select(attrs={
                'class': 'form-select',
                'style': 'width: 200px;',  # 缩短分类框宽度
                'required': True,  # 分类必填
            }),
            'image': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'required': True,  # 图片必填
            }),
            'video_link': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 5,
                'placeholder': '请输入链接地址（可一次输入多条，每条用换行分隔）',
                'required': True,  # 链接地址必填
            }),
            'content': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 10,
                'placeholder': '请输入正文内容',
                'required': True,  # 正文必填
            }),
        }