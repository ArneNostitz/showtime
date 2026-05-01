# Generated manually for streaming_providers field on User model

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0051_user_obfuscate_unseen_episodes"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="streaming_providers",
            field=models.JSONField(
                default=list,
                blank=True,
                help_text="Custom streaming sites. Format: [{'name': 'Site', 'search_url': 'https://.../{slug}'}]",
            ),
        ),
    ]
