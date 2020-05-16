# Generated by Django 3.0.6 on 2020-05-16 07:04

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('clients', '0001_initial'),
        ('agents', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Policy',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255, unique=True)),
                ('desc', models.CharField(max_length=255)),
                ('active', models.BooleanField(default=False)),
                ('agents', models.ManyToManyField(related_name='policies', to='agents.Agent')),
                ('clients', models.ManyToManyField(related_name='policies', to='clients.Client')),
                ('sites', models.ManyToManyField(related_name='policies', to='clients.Site')),
            ],
        ),
    ]
