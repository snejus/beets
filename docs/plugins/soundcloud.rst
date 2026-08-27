SoundCloud Plugin
=================

The ``soundcloud`` plugin provides metadata matches for public SoundCloud_
tracks and releases during import. It uses SoundCloud's documented public API;
it does not download or stream audio.

.. _soundcloud: https://soundcloud.com/

Requirements
------------

SoundCloud requires an Artist Pro subscription to register an API application.
Create an application using the `SoundCloud registration instructions`_, then
copy its client ID and client secret into your beets configuration.

.. _soundcloud registration instructions: https://developers.soundcloud.com/docs/api/register-app

The plugin uses application authentication and can access public resources
only. It does not ask you to sign in to a SoundCloud user account and cannot
match private tracks or playlists.

Configuration
-------------

Enable the plugin and configure your application credentials:

.. code-block:: yaml

    plugins: soundcloud

    soundcloud:
        client_id: YOUR_CLIENT_ID
        client_secret: YOUR_CLIENT_SECRET
        tokenfile: soundcloud_token.json
        search_limit: 5
        data_source_mismatch_penalty: 0.5

The plugin obtains an access token when it first needs the API. It saves the
access and refresh tokens in the configured token file and refreshes them
automatically. Keep both the client secret and token file private.

.. conf:: client_id

    Client ID issued for your SoundCloud application.

.. conf:: client_secret

    Client secret issued for your SoundCloud application.

.. conf:: tokenfile
    :default: soundcloud_token.json

    File used to store renewable application tokens. Relative paths are
    resolved inside the beets configuration directory.

.. include:: ./shared_metadata_source_config.rst

Matching Behavior
-----------------

SoundCloud represents releases and user-created sets with the same playlist
resource. Automatic album searches include only sets SoundCloud marks as
albums, which prevents ordinary playlists from appearing as release matches.

An explicit URL or URN lookup also accepts an ordinary public playlist. This
allows you to intentionally tag a local group of files from a set:

::

    Enter release ID: https://soundcloud.com/artist/sets/release
    Enter release ID: soundcloud:playlists:123456

Track URLs and URNs work for singleton imports:

::

    Enter release ID: https://soundcloud.com/artist/track
    Enter release ID: soundcloud:tracks:123456

Track credits use SoundCloud's metadata artist when present and fall back to the
uploader username. A set whose tracks have different credits is tagged as a
various-artists release. Track numbering follows the order of the SoundCloud
set.

Metadata
--------

The plugin imports titles, artists, track order, duration, release dates,
genres, labels, ISRCs, BPM, musical key, source URLs, and artwork URLs when
SoundCloud provides them. The :doc:`fetchart` plugin can use the release artwork
URL during import.

It also stores these flexible attributes:

.. list-table::
    :header-rows: 1

    - - Attribute
      - Stored on
      - Description
    - - ``soundcloud_track_urn``
      - Item
      - SoundCloud track identifier
    - - ``soundcloud_playlist_urn``
      - Album
      - SoundCloud playlist or set identifier
    - - ``soundcloud_artist_urn``
      - Item and album
      - SoundCloud uploader identifier

Play, like, repost, comment, and download counts are deliberately omitted
because they change independently of an autotagging import.
