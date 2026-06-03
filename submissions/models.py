from django.db import models

# Create your models here.

class Submission(models.Model):
    CATEGORY_CHOICES = [
        (32, '投稿'),
        (33, 'AV'),
        (34, '写真'),
        (35, '素人')
    ]

    title = models.CharField(max_length=255, verbose_name="标题", blank=False, null=False)
    category = models.IntegerField(choices=CATEGORY_CHOICES, verbose_name="分类", blank=False, null=False)
    image = models.ImageField(upload_to='uploads/', verbose_name="封面图", blank=False, null=False)
    video_link = models.TextField(verbose_name="链接地址", blank=False, null=False)
    content = models.TextField(verbose_name="正文", blank=False, null=False)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    def __str__(self):
        return self.title
