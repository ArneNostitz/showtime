# Generated manually for streaming_links field on Item model

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0060_fix_reopened_completed_tv_seasons"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="streaming_links",
            field=models.JSONField(
                default=dict,
                blank=True,
                help_text="User-managed streaming URLs. Format: {'Provider': 'https://...'}",
            ),
        ),
    ]
