from setuptools import find_packages, setup

setup(
    name="TracCronCreateTicket",
    version="1.0.0",
    description="Trac plugin for scheduled ticket creation",
    author="Your Name",
    author_email="your@email.com",
    url="https://github.com/yourname/trac-cron-createticket",
    packages=find_packages(),
    package_data={
        "trac_cron_createticket": ["templates/*.html"],
    },
    include_package_data=True,
    entry_points={
        "trac.plugins": [
            "trac_cron_createticket = trac_cron_createticket",
        ],
    },
    install_requires=[
        "setuptools<70",
        "Trac>=1.6",
        "croniter>=2.0.0",
    ],
    classifiers=[
        "Environment :: Web Environment",
        "Framework :: Trac",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.5",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
)
