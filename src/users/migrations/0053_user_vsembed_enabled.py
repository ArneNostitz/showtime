# Generated manually for vsembed_enabled field on User model

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0052_user_streaming_providers"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="vsembed_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Enable VsEmbed streaming links using IMDb IDs",
            ),
        ),
    ]
